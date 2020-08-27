#!/usr/bin/env bash

source /home/timsey/anaconda3/bin/activate rim

echo "---------------------------------"

CUDA_VISIBLE_DEVICES=0 HDF5_USE_FILE_LOCKING=FALSE python -m src.train_RL_model_sweep \
--dataset fastmri --data-path /home/timsey/HDD/data/fastMRI/singlecoil/ --exp-dir /home/timsey/Projects/mrimpro/exp_results/ --resolution 128 \
--recon-model-checkpoint /home/timsey/Projects/fastMRI-shi/models/unet/al_nounc_res128_8to4in2_cvol_symk/model.pt --recon-model-name nounc \
--of-which-four-pools 0 --num-chans 16 --batch-size 4 --impro-model-name convpool --fc-size 256 --accelerations 8 --acquisition-steps 16 --report-interval 1000 \
--lr 5e-5 --sample-rate 0.5 --seed 0 --num-workers 4 --in-chans 1 --num-epochs 50 --num-pools 4 --pool-stride 1 \
--estimator full_step --num-trajectories 8 --num-dev-trajectories 4 --greedy False --data-range volume --baseline-type selfstep \
--scheduler-type multistep --lr-multi-step-size 10 20 30 40 --lr-gamma .5 --acquisition None --center-volume True --batches-step 4 \
--wandb True --do-train-ssim False

#echo "---------------------------------"
#
#CUDA_VISIBLE_DEVICES=0 HDF5_USE_FILE_LOCKING=FALSE python -m src.train_RL_model_sweep \
#--dataset fastmri --data-path /home/timsey/HDD/data/fastMRI/singlecoil/ --exp-dir /home/timsey/Projects/mrimpro/var_results/RL_selfstep/ --resolution 128 \
#--recon-model-checkpoint /home/timsey/Projects/fastMRI-shi/models/unet/al_nounc_res128_8to4in2_cvol_symk/model.pt --recon-model-name nounc \
#--of-which-four-pools 0 --num-chans 16 --batch-size 4 --impro-model-name convpool --fc-size 256 --accelerations 8 --acquisition-steps 16 --report-interval 100 \
#--lr 1e-4 --sample-rate 0.1 --seed 0 --num-workers 4 --in-chans 1 --num-epochs 30 --num-pools 4 --pool-stride 1 \
#--estimator full_step --num-trajectories 8 --num-dev-trajectories 8 --greedy False --data-range volume --baseline-type selfstep \
#--scheduler-type multistep --lr-multi-step-size 10 20 --lr-gamma .1 --acquisition None --center-volume True --batches-step 4 \
#--wandb True --do-train-ssim True