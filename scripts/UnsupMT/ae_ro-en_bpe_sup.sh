gpus=${1:-2}
output=${2:-UnsupMT2}
mode=${3:-train}
load_from=${4:-none}  # --load_from name --resume
python -m torch.distributed.launch --nproc_per_node=${gpus} --master_port=23456 \
                ez_run.py \
                --prefix [time] \
                --mode ${mode} \
                --data_prefix "/private/home/jgu/data/" \
                --dataset "wmt16" \
                --src "ro" --trg "en" \
                --train_set "train.bpe" \
                --dev_set   "dev.bpe"   \
                --test_set  "test.bpe"  \
                --load_lazy \
                --base "bpe" \
                --workspace_prefix "/private/home/jgu/space/${output}/" \
                --params "t2t-base" \
                --eval_every 500  \
                --batch_size 2048 \
                --inter_size 2 \
                --label_smooth 0.1 \
                --share_embeddings \
                --tensorboard \
                --cross_attn_fashion "forward" \
                --model 'AutoTransformer2' \
                --load_from 10.05_01.33.09..wmt16_t2t-base_ro_en_bpe_0.1_32768_M1_iter=50000 \
                --debug
                # --debug
                # --debug
                # --debug --no_valid
                # --variational \
                #--debug
                #--debug
            

