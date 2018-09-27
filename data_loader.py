"""
-- "Lazy dataloader" for Squirrel --
"""
import math
import torch
import torch.nn as nn
import numpy as np
import time
import os, sys
import torch.distributed as dist
import logging

from torchtext import data, datasets, vocab
from torchtext.data.batch import Batch
from contextlib import ExitStack
from collections import OrderedDict

# ====================== Helper Functions =========================================== #

""" Byte-level Transformation """
def str2byte(string):
    byte = string.encode('utf-8').hex()
    return [byte[k: k+2] for k in range(0, len(byte), 2)]

def byte2str(byte):
    try:
        output = bytes.fromhex(''.join(byte)).decode('utf-8')
    except Exception as e:
        output = ''
    return output


"""" A Lazy text-reader """
def lazy_reader(paths, fields, max_len=None, buffer=16384):  # -- infinite lazy dataloader --
    examples = []
    out_step = 0

    while True:
        
        with ExitStack() as stack:
            files = [stack.enter_context(open(fname, "r", encoding="utf-8")) for fname in paths]         
            for steps, lines in enumerate(zip(*files)):
                
                lines = [line.strip() for line in lines]
                if not any(line == '' for line in lines):
                    if max_len is not None:
                        flag = 0
                        for line in lines:
                            if len(line.split()) > max_len:
                                flag = 1
                                break
                        if flag == 1:
                            continue   

                    examples.append(lines)
                    out_step += 1

                if (out_step % buffer == 0) and (out_step > 0):    # pre-reading the dataset, and cached...
                    # examples = sorted(examples, key=lambda x: sum([len(xi.split()) for xi in x]) )
                    for it, example in enumerate(examples):
                        yield data.Example.fromlist(example, fields)

                    examples = []

"""" A Full text-reader """
def full_reader(paths, fields, max_len=None):
    with ExitStack() as stack:
        files = [stack.enter_context(open(fname, "r", encoding="utf-8")) for fname in paths]
        examples = []
        for steps, lines in enumerate(zip(*files)):
            lines = [line.strip() for line in lines]
            if not any(line == '' for line in lines):
                examples.append(data.Example.fromlist(lines, fields))
        return examples

""" batch fetcher """
def fetch_batch(data, batch_size, batch_size_fn=None, world_size=1, reserve=False):
    """Yield elements from data in chunks of batch_size.
    :: minibatch: a reference of list which the remaining of batches will always be there for fetching next time.
    """

    if batch_size_fn is None:
        def batch_size_fn(new, count, sofar):
            return count

    size_so_far = 0
    t0 = time.time()

    minibatch = []
    if reserve:
        reserved_minibatch = []

    for it, ex in enumerate(data):
        
        if reserve and (it < world_size):
            reserved_minibatch.append(ex)
            continue

        else:
            minibatch.append(ex)
        
        size_so_far = batch_size_fn(ex, len(minibatch), size_so_far)
        if (size_so_far == batch_size * world_size) and (len(minibatch) > world_size):        # make sure there is no empty batches coming out during testing.
            yield minibatch
            minibatch, size_so_far = [], 0
            
        elif (size_so_far > batch_size * world_size) and (len(minibatch) > (world_size + 1)): # make sure there is no empty batches coming out during testing.
            yield minibatch[:-1]
            minibatch, size_so_far = minibatch[-1:], batch_size_fn(ex, 1, 0)

    if reserve:
        minibatch += reserved_minibatch  # make sure there is no empty batches coming out during testing.
    yield minibatch

    
""" pool of batch fetcher """
def fetch_pool(data, batch_size, key, batch_size_fn=lambda new, count, sofar: count, random_shuffler=None, world_size=1):
    """Sort within buckets, then batch, then shuffle batches.
    Partitions data into chunks of size 100*batch_size, sorts examples within
    each chunk using sort_key, then batch these examples and shuffle the
    batches.
    """
    if random_shuffler is None:
        random_shuffler = random.shuffle

    for p in fetch_batch(data, batch_size * 100, batch_size_fn):
        p_batch = fetch_batch(sorted(p, key=key), batch_size, batch_size_fn, world_size, True) 
        for b in random_shuffler(list(p_batch)):
            yield b


# ====================== Supportive Functions =========================================== #

""" sequence data field """
class Seuqence(data.Field):

    def __init__(self, reverse_tokenize, **kwargs):
        super().__init__(**kwargs)
        self.reverse_tokenizer = reverse_tokenize


    def reverse(self, batch, width=1, return_saved_time=False):
        if not self.batch_first:
            batch.t_()

        with torch.cuda.device_of(batch):
            batch = batch.tolist()

        batch = [[self.vocab.itos[ind] for ind in ex] for ex in batch] # denumericalize

        def trim(s, t):
            sentence = []
            for w in s:
                if w == t:
                    break
                sentence.append(w)
            return sentence

        batch = [trim(ex, self.eos_token) for ex in batch] # trim past frst eos

        def filter_special(tok):
            return tok not in (self.init_token, self.pad_token)
        
        def count(ex):
            n_step = 0
            n_pad  = 0
            n_word = 0

            filtered = []
            decision = []

            for e in ex:
                if e == self.init_token:
                    continue

                if e == self.pad_token:
                    n_pad += 1
                    if n_word > 0:
                        n_step += 1
                        n_word = 0

                else:
                    if n_word < (width - 1):
                        n_word += 1
                        
                    else:
                        n_word = 0
                        n_step += 1
                    
                    if n_word == 1:
                        decision.append(0)
                    else:
                        decision.append(1)

                    filtered.append(e)
            
            saved_time = (n_step + (n_word == 0)) / (1 + len(filtered))
            accuracy = len(filtered) / (len(ex) + 1e-9)
            return filtered, saved_time, accuracy, decision

        if return_saved_time:
            batch_filtered, saved_time, accuracy, decisions = [], [], [], []
            for ex in batch:
                b, s, a, d = count(ex)
                batch_filtered.append(b)
                saved_time.append(s)
                accuracy.append(a)
                decisions.append(d)

        else:
            batch_filtered = [list(filter(filter_special, ex)) for ex in batch]

        output = [self.reverse_tokenizer(ex) for ex in batch_filtered]
        if return_saved_time:
            return output, saved_time, accuracy, decisions

        return output


class Sequence2D(Seuqence):
    """
    A new field to transform input into 2D space.
    """
    def pad(self, minibatch):
        """Pad a batch of examples using this field.
        Pads to self.fix_length if provided, otherwise pads to the length of
        the longest example in the batch. Prepends self.init_token and appends
        self.eos_token if those attributes are not None. Returns a tuple of the
        padded list and a list containing lengths of each example if
        `self.include_lengths` is `True` and `self.sequential` is `True`, else just
        returns the padded list. If `self.sequential` is `False`, no padding is applied.
        """
        minibatch = list(minibatch)
        if not self.sequential:
            return minibatch

        if self.fix_length is None:
            max_len = max(len(x) for x in minibatch) + 2
        else:
            raise NotImplementedError

        padded, lengths = [], []  

        for x in minibatch:
            
            # adding space before <bos> and <eos>
            for i, word in enumerate(x):
                x[i].append(' ')

            # adding <bos> and <eos>
            x = [[self.init_token]] + x + [[self.eos_token]]

            # adding sentence pads
            padded.append(x + [[self.pad_token]] * max(0, max_len - len(x)))
            lengths.append(len(padded[-1]) - max(0, max_len - len(x)))
            
        if self.fix_length is None:
            max_word = max(len(word) for sentence in padded for word in sentence)
        else:
            raise NotImplementedError
        
        padded_word_all = []
        for sentence in padded:
            padded_word = []
            for word in sentence:
                # put "space" "<bos>" "<eos>" to the end.
                # adding word pads.
                padded_word.append(word[:-1] + [self.pad_token] * max(0, max_word - len(word)) + word[-1:])
            
            padded_word_all.append(padded_word)  
        
        if self.include_lengths:
            return (padded_word_all, lengths)
        return padded_word_all
    
    def numericalize(self, arr, device=None):
        """Turn a batch of examples that use this field into a Variable.
        If the field has include_lengths=True, a tensor of lengths will be
        included in the return value.
        Arguments:
            arr (List[List[str]], or tuple of (List[List[str]], List[int])):
                List of tokenized and padded examples, or tuple of List of
                tokenized and padded examples and List of lengths of each
                example if self.include_lengths is True.
            device (str or torch.device): A string or instance of `torch.device`
                specifying which device the Variables are going to be created on.
                If left as default, the tensors will be created on cpu. Default: None.
        """
        if self.include_lengths and not isinstance(arr, tuple):
            raise ValueError("Field has include_lengths set to True, but "
                            "input data is not a tuple of "
                            "(data batch, batch lengths).")
        
        if isinstance(arr, tuple):
            arr, lengths = arr
            lengths = torch.tensor(lengths, dtype=self.dtype, device=device)
        
        if self.use_vocab:
            if self.sequential:
                arr = [[[self.vocab.stoi[char] for char in x] for x in ex] for ex in arr]
            else:
                raise NotImplementedError

            if self.postprocessing is not None:
                arr = self.postprocessing(arr, self.vocab)
        else:
            raise NotImplementedError

        var = torch.tensor(arr, dtype=self.dtype, device=device)

        if self.sequential and not self.batch_first:
            var.t_()
        if self.sequential:
            var = var.contiguous()

        if self.include_lengths:
            return var, lengths
        return var


""" parallel dataset. using the lazy loader for training """
class ParallelDataset(datasets.TranslationDataset):
    """ Define a N-parallel dataset: supports abitriry numbers of input streams"""

    def __init__(self, path=None, exts=None, fields=None, lazy=True, max_len=None, buffer=16384, **kwargs):

        assert len(exts) == len(fields), 'N parallel dataset must match'
        self.N = len(fields)
        paths = tuple(os.path.expanduser(path + x) for x in exts)

        if lazy:  # using lazy dataloader -- cannot be used to construct the vocabulary -- 
            super(datasets.TranslationDataset, self).__init__(lazy_reader(paths, fields, max_len, buffer=buffer), fields, **kwargs)
        else:
            super(datasets.TranslationDataset, self).__init__(full_reader(paths, fields, max_len), fields, **kwargs)

    @classmethod
    def splits(cls, path, train=None, validation=None, test=None, lazy=True, **kwargs):
        train_data = None if train is None else cls(path + train, lazy=lazy, **kwargs)
        val_data = None if validation is None else cls(path + validation, lazy=False, **kwargs)
        test_data = None if test is None else cls(path + test, lazy=False, **kwargs)
        return train_data, val_data, test_data


class DistributedBatch(Batch):

    def __init__(self, data=None, dataset=None, device=None, world_size=1, local_rank=0):
        """Create a Batch from a list of examples."""
        
        if data is not None:
            big_batch_size = len(data)
            mini_batch_size = int(math.floor(big_batch_size / world_size))
            additional_size = int(big_batch_size -  mini_batch_size * world_size)

            start_pos = local_rank if additional_size > local_rank else additional_size
            start_pos = start_pos + local_rank * mini_batch_size
            end_pos = (local_rank + 1) if additional_size > (local_rank + 1) else additional_size
            end_pos = end_pos + (local_rank + 1) * mini_batch_size


            # start_pos = (additional_size + mini_batch_size) * local_rank
            # end_pos = (additional_size + mini_batch_size) * (local_rank + 1)
            data = data[start_pos: end_pos]
            
            self.batch_size = len(data)
            self.dataset = dataset
            self.fields = dataset.fields.keys()  # copy field names
            # print('big batch size', big_batch_size, mini_batch_size, self.batch_size, local_rank, 
            #         mini_batch_size * local_rank, mini_batch_size * local_rank + mini_batch_size )
            
            for (name, field) in dataset.fields.items():
                if field is not None:
                    batch = [getattr(x, name) for x in data]
                    setattr(self, name, field.process(batch, device=device))
                    

""" A lazy verison of bucket iterator which supports saving unread minibatches. """
class LazyBucketIterator(data.BucketIterator):

    def __init__(self, dataset, batch_size, sort_key=None, device=None,
                batch_size_fn=None, train=True, repeat=None, sort=None,
                sort_within_batch=False, distributed=False, rank=0, world_size=1):
        super().__init__(dataset, batch_size, sort_key, device, batch_size_fn, 
                        train, repeat, shuffle=False, sort=sort, sort_within_batch=sort_within_batch)
        
        # self.minibatch = []  # save unfinished batches.
        self.distributed = distributed
        self.rank = rank
        self.world_size = world_size

    def create_batches(self):
        if self.sort:
            self.batches = fetch_batch(self.data(), self.batch_size, self.batch_size_fn, self.world_size, True)
        else:
            self.batches = fetch_pool(self.data(), self.batch_size, self.sort_key, self.batch_size_fn,
                                    random_shuffler=self.random_shuffler, world_size=self.world_size)

    # --- wrap the iterator --- 
    def __iter__(self):

        count = 0
        t0 = time.time()
        while True:
            
            self.init_epoch()
            for idx, minibatch in enumerate(self.batches):
                count += 1
                
                # fast-forward if loaded from state
                if self._iterations_this_epoch > idx:
                    continue

                # --- distributed iterator ---
                # if self.distributed:
                #     if count % self.world_size != self.rank:
                #         continue

                self.iterations += 1
                self._iterations_this_epoch += 1
                if self.sort_within_batch:
                    # NOTE: `rnn.pack_padded_sequence` requires that a minibatch
                    # be sorted by decreasing order, which requires reversing
                    # relative to typical sort keys
                    minibatch.sort(key=self.sort_key, reverse=True)

                yield DistributedBatch(minibatch, self.dataset, self.device, self.world_size, self.rank)

            if not self.repeat:
                return


# ========================= DataLoader for Distributed Transformer ==================================== #
class DataLoader(object):

    def __init__(self, args, logger=None, build_vocab=False, vocab_file=None):

        if logger is None:
            logger = logging.getLogger()

        # -- default setting -- #
        tokenizer = lambda s: s.split() 
        revserse_tokenizer = lambda ex: " ".join(ex)
        sort_key = None
        Field = Seuqence
        
        if args.base == 'byte':
            tokenizer = str2byte
            revserse_tokenizer = byte2str

        elif args.base == 'char':
            if not args.c2:
                tokenizer = lambda s: list(s)
                revserse_tokenizer = lambda ex: "".join(ex)

            else:    
                assert args.base == 'char', "2D grid inputs only works at Character-Level"
                
                tokenizer = lambda s: [list(word) for word in s.split()]
                revserse_tokenizer = lambda ex: "".join(ex)
                sort_key  = lambda ex: data.interleave_keys(sum([len(a) for a in ex.src]), 
                                                            sum([len(b) for b in ex.trg]))
                Field = Sequence2D

        # ----------------------- #

        if args.remove_dec_eos:
            TRG = Field(batch_first=True, tokenize=tokenizer, reverse_tokenize=revserse_tokenizer)
        else:
            TRG = Field(init_token='<init>', eos_token='<eos>', batch_first=True, tokenize=tokenizer, reverse_tokenize=revserse_tokenizer)

        if args.share_embeddings:
            SRC = TRG
        elif args.remove_enc_eos:
            SRC = Field(batch_first=True, tokenize=tokenizer, reverse_tokenize=revserse_tokenizer)
        else:
            SRC = Field(init_token='<init>', eos_token='<eos>', batch_first=True, tokenize=tokenizer, reverse_tokenize=revserse_tokenizer)

        self.SRC, self.TRG = SRC, TRG

        pair = args.src + '-' + args.trg
        data_path = os.path.join(args.data_prefix, args.dataset, pair)
        exts=('.src', '.trg')
        reverse = False

        if not os.path.exists(data_path):

            # translation in a reverse direction #
            pair = args.trg + '-' + args.src
            data_path = os.path.join(args.data_prefix, args.dataset, pair)
            exts=('.trg', '.src')
            reverse = True
            
            if not os.path.exists(data_path):
                raise NotImplementedError
            

        # --- setup dataset (no lazy mode when building the vocab) --- #
        train_data, dev_data, test_data = ParallelDataset.splits(
            path= data_path + '/', lazy=(not build_vocab),
            train=args.train_set, validation=args.dev_set, test=args.test_set, 
            exts=exts, fields=[('src', SRC), ('trg', TRG)],
            buffer=16384 * args.world_size)

        logger.info('setup the dataset.')

        # --- read the vocabulary -- #
        if args.base != 'byte':
            
            if vocab_file is None:
                vocab_name = 'vocab.{}.{}.{}.pt'.format(pair, 's' if args.share_embeddings else 'n', 'c' if args.base == 'char' else 'w')
            else:
                vocab_name = vocab_file

            if build_vocab:
                logger.info('build the vocabulary.')
                if not args.share_embeddings:
                    SRC.build_vocab(train_data, max_size=args.max_vocab_size)
                TRG.build_vocab(train_data, max_size=args.max_vocab_size)
                torch.save([SRC.vocab, TRG.vocab], os.path.join(data_path, vocab_name))
                logger.info('done. {}/{}'.format(len(SRC.vocab), len(TRG.vocab)))
                sys.exit(1)

            logger.info('load saved vocabulary.')

            assert os.path.exists(os.path.join(data_path, vocab_name)), 'need to pre-compute the vocab'
            src_vocab, trg_vocab = torch.load(os.path.join(data_path, vocab_name))
            
            if reverse:
                SRC.vocab = trg_vocab
                TRG.vocab = src_vocab
            else:    
                SRC.vocab = src_vocab
                TRG.vocab = trg_vocab
        
        else:
            SRC.build_vocab([["{0:x}".format(a)] for a in range(256)])
            TRG.build_vocab([["{0:x}".format(a)] for a in range(256)])
    
        args.__dict__.update({'trg_vocab': len(TRG.vocab), 'src_vocab': len(SRC.vocab)})

        # --- dynamic batching function -- #
        def dyn_batch_with_padding(new, i, sofar):
            prev_max_len = sofar / (i - 1) if i > 1 else 0
            t =  max(len(new.src), len(new.trg),  prev_max_len) * i
            return t

        def dyn_batch_without_padding(new, i, sofar):
            return sofar + max(len(new.src), len(new.trg))
        
        def dyn_batch_char2d(new, i, sofar):
            return sofar + max(sum([len(a) for a in new.src]), sum([len(b) for b in new.trg]))

        if args.batch_size == 1:  # speed-test: one sentence per batch.
            batch_size_fn = lambda new, count, sofar: count
        else:
            batch_size_fn = dyn_batch_without_padding
        
        # --- build batch-iterator for Translation tasks. ---
        self.train, self.dev, self.test = None, None, None
        if train_data is not None:
            logger.info("build the training set.")
            self.train = LazyBucketIterator(train_data, 
                                            batch_size=args.batch_size, 
                                            device=args.device,
                                            sort_key=sort_key,
                                            batch_size_fn=batch_size_fn, train=True, 
                                            repeat=None if args.mode == 'train' else False,
                                            sort_within_batch=True, 
                                            distributed=args.distributed, 
                                            rank=args.local_rank, world_size=args.world_size)
        if dev_data is not None:
            logger.info("build the validation set.")
            self.dev = LazyBucketIterator(dev_data, 
                                            batch_size=args.batch_size, 
                                            device=args.device,
                                            sort_key=sort_key,
                                            batch_size_fn=batch_size_fn, train=False, 
                                            repeat = False, 
                                            sort_within_batch=True, 
                                            distributed=args.distributed, 
                                            rank=args.local_rank, world_size=args.world_size)

            # self.dev = data.BucketIterator(dev_data, batch_size=args.batch_size * 2, device=args.device,
            #                                 batch_size_fn=batch_size_fn, train=False)
            
        if test_data is not None: 
            logger.info("build the testing set. (normal iterator is fine)")   
            self.test = data.BucketIterator(test_data, batch_size=args.batch_size, device=args.device,
                                            batch_size_fn=batch_size_fn, train=False)


    