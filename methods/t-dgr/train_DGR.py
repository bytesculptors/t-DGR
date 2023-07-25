import argparse
import math
import threading
import torch
import socket
import datetime
import os

from mlp import MLP
from trainer import Trainer as LearnerTrainer
from metaworld_dataset import MetaworldDataset, VideoDataset
from diffusion import GaussianDiffusion, Trainer as DiffusionTrainer
from unet import TemporalUnet

parser = argparse.ArgumentParser()
parser.add_argument('--batch_size', type=int, default=32)
parser.add_argument('--epochs', type=int, default=300)
parser.add_argument('--steps', type=int, default=10000) # number of training steps for diffusion
parser.add_argument('--timesteps', type=int, default=1000) # timesteps for the diffusion process
parser.add_argument('--horizon', type=int, default=16)
parser.add_argument("--lr", type=float, default=0.0001)
parser.add_argument('--dim', type=int, default=128)
parser.add_argument("--learner_ckpt", type=str, default=None)
parser.add_argument("--gen_ckpt", type=str, default=None)
parser.add_argument("--ratio", type=float, default=0.85) # must be in the range [0, 1)
parser.add_argument("--ckpt_folder", type=str, default=None)
parser.add_argument('--warmup', type=int, default=50000)
parser.add_argument('--dataset', type=str, default='/scratch/cluster/william/metaworld/state_data')
parser.add_argument('--benchmark', type=str, choices=['cw20', 'cw10', 'gcl'], default='cw20')
parser.add_argument('--seed', type=int, default=0)
args = parser.parse_args()

torch.manual_seed(args.seed)

# create runs folder
if not os.path.exists('runs'):
    os.mkdir('runs')

# create ckpts folder
if args.ckpt_folder is None:
    args.ckpt_folder = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + f'-{socket.gethostname()}'
args.ckpt_folder = 'runs/' + args.ckpt_folder

# set benchmark specific settings
if args.benchmark == 'cw20' or args.benchmark == 'cw10':
    env_names = ['hammer-v2', 'push-wall-v2', 'faucet-close-v2', 'push-back-v2', 'stick-pull-v2', 'handle-press-side-v2', 'push-v2', 'shelf-place-v2', 'window-close-v2', 'peg-unplug-side-v2']
else:
    env_names = ['bucket0', 'bucket1', 'bucket2', 'bucket3', 'bucket4', 'bucket5', 'bucket6', 'bucket7', 'bucket8', 'bucket9']
repeats = 2 if args.benchmark == 'cw20' else 1

learner_model = MLP(input=49, output=4).cuda()
generator_model = TemporalUnet(args.horizon, 49, dim=args.dim).cuda()
diffusion = GaussianDiffusion(
    generator_model,
    horizon = args.horizon,
    obs_dim = 39,
    cond_dim = 10,
    timesteps = args.timesteps,   # number of steps
    loss_type = 'l1'    # L1 or L2
).cuda()

learner_trainer = None
generator_trainer = None

# load checkpoints
if args.learner_ckpt is not None:
    learner_trainer = LearnerTrainer(learner_model, MetaworldDataset(f'{args.dataset}/{env_names[0]}'), ckpts_folder=args.ckpt_folder, train_batch_size=args.batch_size, train_lr=args.lr) 
    learner_trainer.load(args.learner_ckpt)
if args.gen_ckpt is not None:
    generator_trainer = DiffusionTrainer(
        diffusion,
        VideoDataset(f'{args.dataset}/{env_names[0]}', num_frames = args.horizon),
        train_batch_size = args.batch_size,
        train_lr = args.lr,
        save_every = 100,
        gradient_accumulate_every = 2,    # gradient accumulation steps
        ema_decay = 0.995,                # exponential moving average decay
        amp = True,                        # turn on mixed precision
        results_folder = args.ckpt_folder
    )
    generator_trainer.load(args.gen_ckpt)

# continual learning
maxi = torch.load(args.dataset + '/maxi.pt')
for repeat in range(repeats):
    for env_name in env_names:
        # get dataset
        learner_dataset = MetaworldDataset(f'{args.dataset}/{env_name}')
        generator_dataset = VideoDataset(f'{args.dataset}/{env_name}', num_frames = args.horizon)

        # add fake generated data
        if learner_trainer is not None and generator_trainer is not None:
            prev_learner = learner_trainer.model
            prev_generator = generator_trainer.ema_model

            num_generated_samples = args.ratio * len(learner_dataset) / (1 - args.ratio)
            num_generated_samples = math.ceil(num_generated_samples / generator_model.horizon)
            if 'cw' in args.benchmark:
                num_of_env_so_far = env_names.index(env_name) if repeat == 0 else len(env_names)
            elif args.benchmark == 'gcl':
                num_of_env_so_far = min(env_names.index(env_name) + 1, len(env_names))
            else:
                assert False

            j = 0
            while num_generated_samples > 0 or j % num_of_env_so_far != 0:
                task_id = j % num_of_env_so_far
                cond = torch.eye(len(env_names))[task_id].cuda()
                sample_batch_size = maxi[task_id] - args.horizon + 2
                traj_time = torch.arange(sample_batch_size).cuda()
                traj_data = prev_generator.sample(task_cond=cond, traj_time=traj_time, batch_size=sample_batch_size)
                
                # turn trajectory into state-action pairs
                image_data = traj_data.view(-1, traj_data.shape[2])
                
                # split image_data into batches
                with torch.no_grad():
                    assert image_data.shape[0] % sample_batch_size == 0
                    idx = 0
                    while idx < image_data.shape[0]:
                        data = image_data[idx:idx+sample_batch_size].cuda()
                        target = prev_learner(data).numpy(force=True)
                        data = data.cpu()

                        assert data.shape[0] == sample_batch_size
                        for i in range(data.shape[0]):
                            learner_dataset.add_item([data[i], target[i]])

                        idx += sample_batch_size

                traj_data, traj_time = traj_data.cpu(), traj_time.cpu()
                assert traj_data.shape[0] == traj_time.shape[0]
                for i in range(traj_data.shape[0]):
                    generator_dataset.add_item([traj_data[i], traj_time[i].unsqueeze(0)])

                # update loop variables
                num_generated_samples -= maxi[task_id] + 1
                j += 1
            
        if learner_trainer is None or generator_trainer is None: # initialize trainers for the first time
            learner_trainer = LearnerTrainer(learner_model, learner_dataset, ckpts_folder=args.ckpt_folder, train_batch_size=args.batch_size, train_lr=args.lr)
            generator_trainer = DiffusionTrainer(
                diffusion,
                generator_dataset,
                train_batch_size = args.batch_size,
                train_lr = args.lr,
                save_every = 100,
                gradient_accumulate_every = 2,    # gradient accumulation steps
                ema_decay = 0.995,                # exponential moving average decay
                amp = True,                        # turn on mixed precision
                results_folder = args.ckpt_folder
            )
        else:
            learner_trainer.load_new_dataset(learner_dataset)
            generator_trainer.load_new_dataset(generator_dataset)

        warmup_steps = args.warmup if env_names.index(env_name) == 0 else 0

        learn_thread = threading.Thread(target=learner_trainer.train, args=(args.epochs,))
        generator_thread = threading.Thread(target=generator_trainer.train, args=(args.steps + warmup_steps,))
        learn_thread.start()
        generator_thread.start()
        learn_thread.join()
        generator_thread.join()

        learner_trainer.save(env_name + f'-{repeat}')

