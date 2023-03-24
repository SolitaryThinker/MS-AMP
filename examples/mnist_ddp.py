# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""The ddp mnist exampe using MS-AMP. It is adapted from https://github.com/pytorch/examples/blob/main/mnist/main.py."""

from __future__ import print_function
import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import datasets, transforms
from torch.optim.lr_scheduler import StepLR
import torch.distributed as dist
import msamp


class Net(nn.Module):
    """The neural network model for mnist."""
    def __init__(self):
        """Constructor."""
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        """Forward function.

        Args:
            x (torch.Tensor): input tensor.

        Returns:
            torch.Tensor: output tensor.
        """
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)
        return output


def train(args, model, device, train_loader, optimizer, epoch):
    """Train the model with given data loader and optimizer.

    Args:
        args (argparse.Namespace): arguments.
        model (torch.nn.Module): the model to train.
        device (torch.device): the device to train on.
        train_loader (torch.utils.data.DataLoader): the data loader for training.
        optimizer (torch.optim.Optimizer): the optimizer to use.
        epoch (int): the number of epoch to run on data loader.
    """
    model.train()
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        if hasattr(optimizer, 'all_reduce_grads'):
            optimizer.all_reduce_grads(model)
        optimizer.step()
        if dist.get_rank() == 0:
            if batch_idx % args.log_interval == 0:
                print(
                    'Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                        epoch,
                        dist.get_world_size() * batch_idx * len(data), len(train_loader.dataset),
                        100. * batch_idx / len(train_loader), loss.item()
                    )
                )
        if args.dry_run:
            break


def test(model, device, test_loader):
    """Test the model on test data set.

    Args:
        model (torch.nn.Module): the model to test.
        device (torch.device): the device to test on.
        test_loader (torch.utils.data.DataLoader): the data loader for testing.
    """
    model.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item()    # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True)    # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)

    print(
        '\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
            test_loss, correct, len(test_loader.dataset), 100. * correct / len(test_loader.dataset)
        )
    )


def main():
    """The main function."""
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
    parser.add_argument(
        '--batch-size', type=int, default=64, metavar='N', help='input batch size for training (default: 64)'
    )
    parser.add_argument(
        '--test-batch-size', type=int, default=1000, metavar='N', help='input batch size for testing (default: 1000)'
    )
    parser.add_argument('--epochs', type=int, default=4, metavar='N', help='number of epochs to train (default: 4)')
    parser.add_argument('--lr', type=float, default=3e-4, metavar='LR', help='learning rate (default: 1.0)')
    parser.add_argument('--gamma', type=float, default=0.7, metavar='M', help='Learning rate step gamma (default: 0.7)')
    parser.add_argument('--no-cuda', action='store_true', default=False, help='disables CUDA training')
    parser.add_argument('--dry-run', action='store_true', default=False, help='quickly check a single pass')
    parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')
    parser.add_argument(
        '--log-interval',
        type=int,
        default=10,
        metavar='N',
        help='how many batches to wait before logging training status'
    )
    parser.add_argument('--save-model', action='store_true', default=False, help='For Saving the current Model')

    parser.add_argument('--local_rank', type=int, help='local rank, will passed by ddp')

    parser.add_argument('--enable-msamp', action='store_true', default=False, help='enable MS-AMP')
    parser.add_argument('--opt-level', type=str, default='O1', help='MS-AMP optimization level')

    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    torch.cuda.set_device(args.local_rank)
    dist.init_process_group(backend='nccl', init_method='env://')

    torch.manual_seed(args.seed)

    device = torch.device('cuda' if use_cuda else 'cpu')

    train_kwargs = {'batch_size': args.batch_size}
    test_kwargs = {'batch_size': args.test_batch_size}
    if use_cuda:
        cuda_kwargs = {
            'num_workers': 1,
            'pin_memory': True,
        }
        train_kwargs.update(cuda_kwargs)
        test_kwargs.update(cuda_kwargs)

    transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307, ), (0.3081, ))])

    if args.local_rank == 0:
        dataset1 = datasets.MNIST('./data', train=True, download=True, transform=transform)
    torch.cuda.synchronize()
    if args.local_rank > 0:
        dataset1 = datasets.MNIST('./data', train=True, download=False, transform=transform)

    dataset2 = datasets.MNIST('./data', train=False, transform=transform)

    train_sampler = torch.utils.data.distributed.DistributedSampler(dataset1, shuffle=True)

    test_sampler = torch.utils.data.SequentialSampler(dataset2)

    train_loader = torch.utils.data.DataLoader(dataset1, sampler=train_sampler, shuffle=False, **train_kwargs)
    test_loader = torch.utils.data.DataLoader(dataset2, sampler=test_sampler, **test_kwargs)

    model = Net().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)

    if args.enable_msamp:
        print(f'msamp is enabled, opt_level: {args.opt_level}')
        model, optimizer = msamp.initialize(model, optimizer, opt_level=args.opt_level)

    model = torch.nn.parallel.DistributedDataParallel(
        model, device_ids=[args.local_rank], output_device=args.local_rank
    )

    scheduler = StepLR(optimizer, step_size=1, gamma=args.gamma)
    for epoch in range(1, args.epochs + 1):
        train_sampler.set_epoch(epoch)
        train(args, model, device, train_loader, optimizer, epoch)
        if dist.get_rank() == 0:
            test(model.module, device, test_loader)
        scheduler.step()

    if args.save_model:
        if dist.get_rank() == 0:
            torch.save(model.state_dict(), 'mnist_cnn.pt')


if __name__ == '__main__':
    main()