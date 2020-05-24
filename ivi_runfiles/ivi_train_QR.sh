#!/bin/sh

#SBATCH --ntasks=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=10
#SBATCH --priority=TOP
#SBATCH --mem=10G
#SBATCH --verbose
#SBATCH --time 5-0:00:00
#SBATCH --job-name=greedy

#SBATCH -D /home/tbbakke/mrimpro

echo "Running..."

source /home/tbbakke/anaconda3/bin/activate ml

nvidia-smi

# Do your stuff

# 1033
CUDA_VISIBLE_DEVICES=0 HDF5_USE_FILE_LOCKING=FALSE python -m src.train_improQR_model_sweep \
--data-path /home/tbbakke/data/fastMRI/singlecoil/ \
--recon-model-checkpoint /home/tbbakke/fastMRI-shi/models/unet/al_nounc_res128_8to4in2_cvol_symk/model.pt \
--wandb True \
--resume True --run_id 16f367ps --impro-model-checkpoint /home/tbbakke/mrimpro/exp_results/res128_al28_accel[32]_convpool_nounc_k16_2020-05-21_01:18:40/model.pt

# 1046
#CUDA_VISIBLE_DEVICES=0 HDF5_USE_FILE_LOCKING=FALSE python -m src.train_improQR_model_sweep \
#--data-path /home/tbbakke/data/fastMRI/singlecoil/ \
#--recon-model-checkpoint /home/tbbakke/fastMRI-shi/models/unet/al_nounc_res128_8to4in2_cvol_symk/model.pt \
#--wandb True \
#--resume True --run_id x8f8areg --impro-model-checkpoint /home/tbbakke/mrimpro/exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-22_13:10:25/model.pt

# 1048
#CUDA_VISIBLE_DEVICES=0 HDF5_USE_FILE_LOCKING=FALSE python -m src.train_improQR_model_sweep \
#--data-path /home/tbbakke/data/fastMRI/singlecoil/ \
#--recon-model-checkpoint /home/tbbakke/fastMRI-shi/models/unet/al_nounc_res128_8to4in2_cvol_symk/model.pt \
#--wandb True  \
#--resume True --run_id 2bgdrs2f --impro-model-checkpoint /home/tbbakke/mrimpro/exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-22_13:10:53/model.pt