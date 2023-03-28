import os
import hydra
from typing import Tuple
from omegaconf import DictConfig
from torch import FloatTensor
from torch.nn import MSELoss
from torch.optim import Adam, lr_scheduler
from torch.utils.data import DataLoader

from modulus.models import FullyConnected, FNO
from modulus.distributed import DistributedManager
from modulus.utils import StaticCaptureTraining, StaticCaptureEvaluateNoGrad
from modulus.launch.utils import load_checkpoint, save_checkpoint
from modulus.launch.logging import PythonLogger, LaunchLogger, initialize_mlflow

from utils import NestedDarcyDataset, GridValidator


def InitializeLoggers(cfg: DictConfig) -> Tuple[DistributedManager, PythonLogger]:
    """Class containing most important objects

    In this class the infrastructure for training is set.

    Parameters
    ----------
    cfg : DictConfig
        config file parameters

    Returns
    -------
    Tuple[DistributedManager, PythonLogger]
    """
    DistributedManager.initialize() # Only call this once in the entire script!
    dist = DistributedManager()     # call if required elsewhere
    logger = PythonLogger(name='darcy_nested_fno')

    assert hasattr(cfg, 'model'), \
        logger.error(f'define which model to train: $ python {__file__.split(os.sep)[-1]} +model=<model_name>')
    logger.info(f'training model {cfg.model}')

    # initialize monitoring
    initialize_mlflow(
        experiment_name=f'Nested FNO, model: {cfg.model}',
        experiment_desc=f'training model {cfg.model} for nested FNOs',
        run_name=f'Nested FNO training, model: {cfg.model}',
        run_desc=f'training model {cfg.model} for nested FNOs',
        user_name='Gretchen Ross',
        mode='offline',
    )
    LaunchLogger.initialize(use_mlflow=True)  # Modulus launch logger

    return dist, logger

class SetUpInfrastructure:
    """Class containing most important objects

    In this class the infrastructure for training is set.

    Parameters
    ----------
    cfg : DictConfig
        config file parameters
    dist : DistributedManager
        persistance class instance for storing parallel environment information
    logger : PythonLogger
        logger for command line output
    """
    def __init__(self,
                 cfg: DictConfig,
                 dist: DistributedManager,
                 logger: PythonLogger,
                 parent_prediction: FloatTensor=None) -> None:
        # define model, loss, optimiser, scheduler, data loader
        level = int(cfg.model[-1])
        model_cfg = cfg.arch[cfg.model]
        loss_fun  = MSELoss(reduction='mean')
        norm      = {'permeability': (cfg.normaliser.permeability.mean, cfg.normaliser.permeability.std),
                     'darcy':        (cfg.normaliser.darcy.mean,        cfg.normaliser.darcy.std)}

        self.training_set = NestedDarcyDataset(mode='train', data_path=cfg.training.training_set,
                                               level=level, norm=norm, log=logger)
        self.valid_set    = NestedDarcyDataset(mode='train', data_path=cfg.validation.validation_set,
                                               level=level, norm=norm, log=logger)
        self.train_loader = DataLoader(self.training_set, batch_size=cfg.training.batch_size, shuffle=True)
        self.valid_loader = DataLoader(self.valid_set, batch_size=cfg.validation.batch_size, shuffle=False)
        self.validator    = GridValidator(loss_fun=loss_fun, norm=norm)
        decoder           = FullyConnected(in_features=model_cfg.fno.latent_channels,
                                      out_features=model_cfg.decoder.out_features,
                                      num_layers=model_cfg.decoder.layers,
                                      layer_size=model_cfg.decoder.layer_size)
        self.model        = FNO(decoder_net=decoder,
                                in_channels=model_cfg.fno.in_channels,
                                dimension=model_cfg.fno.dimension,
                                latent_channels=model_cfg.fno.latent_channels,
                                num_fno_layers=model_cfg.fno.fno_layers,
                                num_fno_modes=model_cfg.fno.fno_modes,
                                padding=model_cfg.fno.padding).to(dist.device)
        self.optimizer    = Adam(self.model.parameters(), lr=cfg.scheduler.initial_lr)
        self.scheduler    = lr_scheduler.LambdaLR(
                               self.optimizer, lr_lambda=lambda step: cfg.scheduler.decay_rate**step)
        self.log_args     = {'name_space': 'train',
                             'num_mini_batch': len(self.train_loader),
                             'epoch_alert_freq': 1}
        self.ckpt_args    = {'path': f'./checkpoints/{cfg.model}',
                             'optimizer': self.optimizer,
                             'scheduler': self.scheduler,
                             'models': self.model}

        # define forward for training and inference
        @StaticCaptureTraining(model=self.model, optim=self.optimizer, logger=logger, use_amp=False, use_graphs=False)
        def _forward_train(invars, target):
            pred = self.model(invars)
            loss = loss_fun(pred, target)
            return loss

        @StaticCaptureEvaluateNoGrad(model=self.model, logger=logger, use_amp=False, use_graphs=False)
        def _forward_eval(invars):
            return self.model(invars)

        self.forward_train = _forward_train
        self.forward_eval  = _forward_eval


def TrainModel(cfg: DictConfig,
               base: SetUpInfrastructure,
               loaded_epoch: int) -> None:
    """Training Loop

    Parameters
    ----------
    cfg : DictConfig
        config file parameters
    base : SetUpInfrastructure
        important objects
    loaded_epoch : int
        epoch from which training is restarted, ==0 if starting from scratch
    """

    for epoch in range(max(1,loaded_epoch+1), cfg.training.max_epochs):
        # Wrap epoch in launch logger for console / MLFlow logs
        with LaunchLogger(**base.log_args, epoch=epoch) as log:
            for batch in base.train_loader:
                loss = base.forward_train(batch['permeability'], batch['darcy'])
                log.log_minibatch({'loss': loss.detach()})
            log.log_epoch({'Learning Rate': base.optimizer.param_groups[0]['lr']})

        # save checkpoint
        if (epoch+1)%cfg.training.rec_results_freq == 0:
            save_checkpoint(**base.ckpt_args, epoch=epoch)

        # validation
        if (epoch+1)%cfg.validation.validation_epochs == 0:
            with LaunchLogger('valid', epoch=epoch) as log:
                total_loss = 0.
                for batch in base.valid_loader:
                    loss = base.validator.compare(batch['permeability'], batch['darcy'],
                                    base.forward_eval(batch["permeability"]), epoch+1)
                    total_loss += loss*batch['darcy'].shape[0]/len(base.valid_set)
                log.log_epoch({'Validation error': total_loss})

        # update learning rate
        if (epoch+1)%cfg.scheduler.decay_epochs==0:
            base.scheduler.step()

    # save final checkpoint
    save_checkpoint(**base.ckpt_args, epoch=cfg.training.max_epochs-1)


def EvaluateParent() -> FloatTensor:


    return pred


@hydra.main(version_base="1.3", config_path=".", config_name="config.yaml")
def nested_darcy_trainer(cfg: DictConfig) -> None:
    """Training for the 2D nested Darcy flow problem.

    This training script demonstrates how to set up a data-driven model for a nested 2D Darcy flow
    using nested Fourier Neural Operators (nFNO). nFNOs are basically a concatination of individual
    FNO models. Individual FNOs can be trained independently and in any order. The order only gets
    important for fine tuning (tba) and inference.
    """

    # initialize loggers
    dist, logger = InitializeLoggers(cfg) #TODO add "train" or "finetune" for output and logger name, use from train_
    log = PythonLogger(name='darcy_fno')

    model_names = sorted(list(cfg.arch.keys()))
    norm = {'permeability': (cfg.normaliser.permeability.mean, cfg.normaliser.permeability.std),
            'darcy':        (cfg.normaliser.darcy.mean,        cfg.normaliser.darcy.std)}

    # evaluate parent
    ..., result = EvaluateModel(cfg, name, norm, result, log) # TODO eventually use the one from eval.py

    # set up infrastructure, result
    base = SetUpInfrastructure(cfg, dist, logger, parent_prediction) #TODO reuse from train.py

    # catch restart if finetune checkpoint exists, else load training checkpoint TODO
    loaded_epoch = load_checkpoint(**base.ckpt_args, device=dist.device)
    if loaded_epoch == 0:
        logger.success('Training started...')
    else:
        logger.warning(f'Resuming training from epoch {loaded_epoch+1}.')

    # train model
    TrainModel(cfg, base, loaded_epoch)
    logger.success('Fine tuning completed *yay*')

if __name__ == '__main__':
    nested_darcy_trainer()
