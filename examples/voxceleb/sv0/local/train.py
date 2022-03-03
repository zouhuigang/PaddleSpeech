# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import os

import numpy as np
import paddle
from paddle.io import DataLoader
from paddle.io import DistributedBatchSampler

from paddleaudio.datasets.voxceleb import VoxCeleb1
from paddleaudio.features.core import melspectrogram
from paddleaudio.utils.time import Timer
from paddlespeech.vector.datasets.batch import feature_normalize
from paddlespeech.vector.datasets.batch import waveform_collate_fn
from paddlespeech.vector.layers.loss import AdditiveAngularMargin
from paddlespeech.vector.layers.loss import LogSoftmaxWrapper
from paddlespeech.vector.layers.lr import CyclicLRScheduler
from paddlespeech.vector.models.ecapa_tdnn import EcapaTdnn
from paddlespeech.vector.training.sid_model import SpeakerIdetification

# feat configuration
cpu_feat_conf = {
    'n_mels': 80,
    'window_size': 400,
    'hop_length': 160,
}


def main(args):
    # stage0: set the training device, cpu or gpu
    paddle.set_device(args.device)

    # stage1: we must call the paddle.distributed.init_parallel_env() api at the begining
    paddle.distributed.init_parallel_env()
    nranks = paddle.distributed.get_world_size()
    local_rank = paddle.distributed.get_rank()

    # stage2: data prepare
    # note: some cmd must do in rank==0
    train_ds = VoxCeleb1('train', target_dir=args.data_dir)
    dev_ds = VoxCeleb1('dev', target_dir=args.data_dir)

    # stage3: build the dnn backbone model network
    #"channels": [1024, 1024, 1024, 1024, 3072],
    model_conf = {
        "input_size": 80,
        "channels": [512, 512, 512, 512, 1536],
        "kernel_sizes": [5, 3, 3, 3, 1],
        "dilations": [1, 2, 3, 4, 1],
        "attention_channels": 128,
        "lin_neurons": 192,
    }
    ecapa_tdnn = EcapaTdnn(**model_conf)

    # stage4: build the speaker verification train instance with backbone model
    model = SpeakerIdetification(
        backbone=ecapa_tdnn, num_class=VoxCeleb1.num_speakers)

    # stage5: build the optimizer, we now only construct the AdamW optimizer
    lr_schedule = CyclicLRScheduler(
        base_lr=args.learning_rate, max_lr=1e-3, step_size=140000 // nranks)
    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_schedule, parameters=model.parameters())

    # stage6: build the loss function, we now only support LogSoftmaxWrapper
    criterion = LogSoftmaxWrapper(
        loss_fn=AdditiveAngularMargin(margin=0.2, scale=30))

    # stage7: confirm training start epoch
    #         if pre-trained model exists, start epoch confirmed by the pre-trained model
    start_epoch = 0
    if args.load_checkpoint:
        args.load_checkpoint = os.path.abspath(
            os.path.expanduser(args.load_checkpoint))
        try:
            # load model checkpoint
            state_dict = paddle.load(
                os.path.join(args.load_checkpoint, 'model.pdparams'))
            model.set_state_dict(state_dict)

            # load optimizer checkpoint
            state_dict = paddle.load(
                os.path.join(args.load_checkpoint, 'model.pdopt'))
            optimizer.set_state_dict(state_dict)
            if local_rank == 0:
                print(f'Checkpoint loaded from {args.load_checkpoint}')
        except FileExistsError:
            if local_rank == 0:
                print('Train from scratch.')

        try:
            start_epoch = int(args.load_checkpoint[-1])
            print(f'Restore training from epoch {start_epoch}.')
        except ValueError:
            pass

    # stage8: we build the batch sampler for paddle.DataLoader
    train_sampler = DistributedBatchSampler(
        train_ds, batch_size=args.batch_size, shuffle=True, drop_last=False)
    train_loader = DataLoader(
        train_ds,
        batch_sampler=train_sampler,
        num_workers=args.num_workers,
        collate_fn=waveform_collate_fn,
        return_list=True,
        use_buffer_reader=True, )

    # stage9: start to train
    #         we will comment the training process
    steps_per_epoch = len(train_sampler)
    timer = Timer(steps_per_epoch * args.epochs)
    timer.start()

    for epoch in range(start_epoch + 1, args.epochs + 1):
        # at the begining, model must set to train mode
        model.train()

        avg_loss = 0
        num_corrects = 0
        num_samples = 0
        for batch_idx, batch in enumerate(train_loader):
            waveforms, labels = batch['waveforms'], batch['labels']

            feats = []
            for waveform in waveforms.numpy():
                feat = melspectrogram(x=waveform, **cpu_feat_conf)
                feats.append(feat)
            feats = paddle.to_tensor(np.asarray(feats))
            feats = feature_normalize(
                feats, mean_norm=True, std_norm=False)  # Features normalization
            logits = model(feats)

            loss = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            if isinstance(optimizer._learning_rate,
                          paddle.optimizer.lr.LRScheduler):
                optimizer._learning_rate.step()
            optimizer.clear_grad()

            # Calculate loss
            avg_loss += loss.numpy()[0]

            # Calculate metrics
            preds = paddle.argmax(logits, axis=1)
            num_corrects += (preds == labels).numpy().sum()
            num_samples += feats.shape[0]

            timer.count()

            if (batch_idx + 1) % args.log_freq == 0 and local_rank == 0:
                lr = optimizer.get_lr()
                avg_loss /= args.log_freq
                avg_acc = num_corrects / num_samples

                print_msg = 'Epoch={}/{}, Step={}/{}'.format(
                    epoch, args.epochs, batch_idx + 1, steps_per_epoch)
                print_msg += ' loss={:.4f}'.format(avg_loss)
                print_msg += ' acc={:.4f}'.format(avg_acc)
                print_msg += ' lr={:.4E} step/sec={:.2f} | ETA {}'.format(
                    lr, timer.timing, timer.eta)
                print(print_msg)

                avg_loss = 0
                num_corrects = 0
                num_samples = 0

        if epoch % args.save_freq == 0 and batch_idx + 1 == steps_per_epoch:
            if local_rank != 0:
                paddle.distributed.barrier(
                )  # Wait for valid step in main process
                continue  # Resume trainning on other process

            dev_sampler = paddle.io.BatchSampler(
                dev_ds,
                batch_size=args.batch_size // 4,
                shuffle=False,
                drop_last=False)
            dev_loader = paddle.io.DataLoader(
                dev_ds,
                batch_sampler=dev_sampler,
                collate_fn=waveform_collate_fn,
                num_workers=args.num_workers,
                return_list=True, )

            model.eval()
            num_corrects = 0
            num_samples = 0
            print('Evaluate on validation dataset')
            with paddle.no_grad():
                for batch_idx, batch in enumerate(dev_loader):
                    waveforms, labels = batch['waveforms'], batch['labels']
                    # feats = feature_extractor(waveforms)
                    feats = []
                    for waveform in waveforms.numpy():
                        feat = melspectrogram(x=waveform, **cpu_feat_conf)
                        feats.append(feat)
                    feats = paddle.to_tensor(np.asarray(feats))
                    feats = feature_normalize(
                        feats, mean_norm=True, std_norm=False)
                    logits = model(feats)

                    preds = paddle.argmax(logits, axis=1)
                    num_corrects += (preds == labels).numpy().sum()
                    num_samples += feats.shape[0]

            print_msg = '[Evaluation result]'
            print_msg += ' dev_acc={:.4f}'.format(num_corrects / num_samples)

            print(print_msg)

            # Save model
            save_dir = os.path.join(args.checkpoint_dir,
                                    'epoch_{}'.format(epoch))
            print('Saving model checkpoint to {}'.format(save_dir))
            paddle.save(model.state_dict(),
                        os.path.join(save_dir, 'model.pdparams'))
            paddle.save(optimizer.state_dict(),
                        os.path.join(save_dir, 'model.pdopt'))

            if nranks > 1:
                paddle.distributed.barrier()  # Main process


if __name__ == "__main__":
    # yapf: disable
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--device',
                        choices=['cpu', 'gpu'],
                        default="cpu",
                        help="Select which device to train model, defaults to gpu.")
    parser.add_argument("--data-dir",
                        default="./data/",
                        type=str,
                        help="data directory")
    parser.add_argument("--learning-rate",
                        type=float,
                        default=1e-8,
                        help="Learning rate used to train with warmup.")
    parser.add_argument("--load-checkpoint",
                        type=str,
                        default=None,
                        help="Directory to load model checkpoint to contiune trainning.")
    parser.add_argument("--batch-size",
                        type=int, default=64,
                        help="Total examples' number in batch for training.")
    parser.add_argument("--num-workers",
                        type=int,
                        default=0,
                        help="Number of workers in dataloader.")
    parser.add_argument("--epochs",
                        type=int,
                        default=50,
                        help="Number of epoches for fine-tuning.")
    parser.add_argument("--log_freq",
                        type=int,
                        default=10,
                        help="Log the training infomation every n steps.")

    args = parser.parse_args()
    # yapf: enable

    main(args)
