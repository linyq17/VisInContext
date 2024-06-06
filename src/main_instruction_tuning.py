"""
Load pre-trained model and fine-tuning on downstream dataset.
"""

MODEL_VERSION_CONFIG_FILE_PATH = 'src/config/model_version/model_version.yaml'
WANDB_PARAMS = ['wandb_project', 'wandb_watch', 'wandb_log_model', 'wandb_dir']

import os
import argparse
import torch
import transformers
import yaml
import numpy as np
import random
import warnings
warnings.filterwarnings("ignore", category=UserWarning) # ignore UserWarning from webdataset
from transformers.integrations import WandbCallback
import wandb

from utils import save_config_and_src, device_info, CustomLogger, has_checkpoints
from multimodal_model import CustomTrainer
from data import instruction_dataloader
import importlib
from safetensors.torch import load_file
with open(MODEL_VERSION_CONFIG_FILE_PATH) as f:
    config = yaml.safe_load(f)
model_version = config['load_model']['version']
model_module = importlib.import_module(f'multimodal_model.load_model{model_version}')
create_cosmo = getattr(model_module, 'create_cosmo')


def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)  # if you are using multi-GPU.
    np.random.seed(seed)  # Numpy module.
    random.seed(seed)  # Python random module.
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    

# set_seed(0)  # set seed for reproducibility

def load_config(base_path, variant_path):
    """
    If variant_path is not None, recursively update the base config with the variant config.
    """
    with open(base_path, 'r') as base_file:
        config = yaml.safe_load(base_file)
    if variant_path is None:
        return config
    with open(variant_path, 'r') as variant_file:
        variant_config = yaml.safe_load(variant_file)

    # Recursively update the base config with the variant config
    def update_config(base, variant):
        for key, value in variant.items():
            if isinstance(value, dict):
                base[key] = update_config(base.get(key, {}), value)
            else:
                base[key] = value
        return base

    return update_config(config, variant_config)

def set_wandb_params(wandb_params, use_wandb=True):
    if use_wandb:
        for param in WANDB_PARAMS:
            if len(wandb_params[param]) > 0:
                os.environ[param.upper()] = wandb_params[param]
    os.environ["DISABLE_MLFLOW_INTEGRATION"] = "TRUE"
    os.environ["WANDB__SERVICE_WAIT"] = "300"

def compute_metrics(val_metrics):
    """
    the eval_preds is the top1 accuracy
    """
    metrics = {
        'lm_top1': val_metrics['top1'],
        'lm_top5': val_metrics['top5'],
        'eval_loss': val_metrics['eval_loss']
    }
    return metrics

def main(args):
    config = load_config(args.base_config, args.variant_config)
    model_params = config['model_params']
    dataset_params = config['dataset_params']
    training_params = config['training_params']
    wandb_params = config['wandb_params']
    lora_params = config['lora_params']
    setting = config['setting']
    data_setting = config['data_setting']
    training_params['data_resampling'] = True # the dataloader is baed on pytorch, so we need to set data_resampling to True
    data_path_prefix = data_setting['local_prefix']
    # azfuse will upload the model to blob if use_azfuse is True
    if dataset_params['use_azfuse']:
        upload_model_to_blob = dataset_params['upload_model_to_blob']
        print("upload_model_to_blob: ", upload_model_to_blob)
    else:
        upload_model_to_blob = False
    # ======================================== custom_logger.info device info, save logs, init wandb on work 0 ===========================
    # Usage:
    args = parser.parse_args()
    # ========================================== wandb config ===========================================
    # Check if parameter passed or if set within environ
    use_wandb = wandb_params["wandb"]
    set_wandb_params(wandb_params, use_wandb)

    # ========================================== gradient accumulate config ===========================================
    # it's best to larger than dataloader nums
    # num_data_types = sum(bool(data_setting['train'][key]) for key in data_setting['train'].keys())
    def count_use_true_entries(d):
        count = 0
        for key, value in d.items():
            if isinstance(value, dict):
                count += count_use_true_entries(value)
            elif key.startswith("use_") and value is True and key != "use_azfuse":
                count += 1
        return count
    num_type_use_true = count_use_true_entries(dataset_params)
    gradient_accumulation_steps = num_type_use_true
    # print("gradient_accumulation_steps: ", gradient_accumulation_steps)
    assert gradient_accumulation_steps % num_type_use_true == 0, "gradient_accumulation_steps should be divisible by data types used"

    # ========================================== model define ===========================================
    model, image_processor, video_processor, text_tokenizer = create_cosmo(model_params, lora_params, instruction_tuning=True)

    # load the model from the checkpoint without tokenizers (since we add new special token for instrcution tuning)
    ckpt_path = config['general'].get('ckpt_path', None)
    if ckpt_path is None:
        print("!!no ckpt_path is given, use the default ckpt_path")
    else:
        print(f"!!load ckpt from: {ckpt_path}")
        if ckpt_path.endswith('.safetensors'): # HF checkpoint
            ckpt = load_file(ckpt_path)
        else:
            ckpt = torch.load(ckpt_path, map_location=torch.device('cpu'))
        # check if the model include 'lang_model.model.decoder.embed_tokens.weight'
    # other models
    if 'lang_model.model.decoder.embed_tokens.weight' in ckpt:
        del ckpt['lang_model.model.decoder.embed_tokens.weight']
    # mixtral 7x8b
    if 'lang_model.model.embed_tokens.weight' in ckpt:
        del ckpt['lang_model.model.embed_tokens.weight']
    if 'lang_model.lm_head.weight' in ckpt:
        del ckpt['lang_model.lm_head.weight']

        model.load_state_dict(ckpt, strict=False) # True


    # ========================================== model training ===========================================
    # in the deepspeed, most are set to auto means replace by our custom defined
    # for wandb, we need to call the model without give wandb_agent
    # more details here https://docs.wandb.ai/guides/track/log/distributed-training
    # *** Notice that deepspeed will replace the optimizer and lr_scheduler
    # so we remove the lr_scheduler in deepspeed_config.json as auto and define here (compare deepspeed_config.json with deepspeed_config_wo_zero.json)
    # need to set optimizer in the deepspeed_config.json
    # ** not sure if will let deepspeed slow down
    trainer = CustomTrainer(
        model=model,
        model_params=model_params,
        training_params=training_params,
        compute_metrics=compute_metrics,
        upload_model_to_blob=upload_model_to_blob,
        args=transformers.TrainingArguments(
            deepspeed=args.deepspeed,
            per_device_train_batch_size=training_params['micro_batch_size'],
            gradient_accumulation_steps=gradient_accumulation_steps,
            num_train_epochs=training_params['num_epochs'],
            learning_rate=training_params['learning_rate'],
            lr_scheduler_type=training_params['lr_scheduler_type'],
            metric_for_best_model="eval_loss",
            ignore_data_skip=training_params['ignore_data_skip'],
            warmup_steps=training_params['warmup_steps'],
            warmup_ratio=training_params['warmup_ratio'],
            fp16=True,
            logging_steps=training_params['logging_steps'],
            optim=training_params['optim'],
            evaluation_strategy="steps" if training_params['eval'] else "no",
            eval_steps=training_params['eval_steps'] if training_params['eval'] else None,
            save_strategy="steps",
            save_steps=training_params['save_steps'],
            save_total_limit=training_params['save_total_limit'],
            output_dir=setting['output_dir'],
            load_best_model_at_end=False
        )
    )
    # if load_best_model_at_end=True, then the  save_on_each_node=True should also be set
    # the wandbcallback writen by HF have some bugs, so we need to remove it and write our own
    trainer.remove_callback(WandbCallback)
    global_rank = torch.distributed.get_rank()
    is_main = (global_rank == 0)
    custom_logger = CustomLogger(global_rank)
    trainer.custom_logger = custom_logger
    if is_main:
        custom_logger.info_w_delimiter("device info: ", color='green')
        if torch.cuda.device_count() > 4:
            device_info()
        # ======================================== save config&src into log files ===========================
        custom_logger.info_w_delimiter("config: {}".format(config), color='green')
        try:
            wandb_agent = wandb.init(project=os.environ["WANDB_PROJECT"], config=config, group="DDP", name=wandb_params['wandb_run_name']) if use_wandb else None
        except Exception as e:
            print("wandb init error: ", e)
            wandb_agent = None
    else:
        wandb_agent = None
    trainer.wandb_agent = wandb_agent

    specific_dir = save_config_and_src(config, setting['src_dir'], setting['output_dir'], args.base_config, args.variant_config, add_time_stamp=setting['add_time_stamp'])
    setting['output_dir'] = specific_dir
    custom_logger.info("config, src and trained models will saved into {}".format(specific_dir))
    trainer.args.output_dir = specific_dir
    resume_from_checkpoint=has_checkpoints(specific_dir)
    
    # ========================================== custom dataloader ===========================================
    custom_logger.info("*"*100, color="red")
    custom_logger.info_w_delimiter("start loading data...", color='green')
    dataloader_config = {
        'tokenizer':text_tokenizer,
        'image_processor': image_processor,
        'video_processor': video_processor,
        'dataset_params': dataset_params,
        'custom_logger': custom_logger
    }

    train_dataloader = instruction_dataloader(training_params['micro_batch_size'], training_params['workers'], data_setting['train'], 
                                                  prefix=data_path_prefix,
                                                  split='train',
                                                  config=dataloader_config,
                                                  custom_logger=custom_logger,
                                                  dataset_params=dataset_params)
    if training_params['eval']:
        val_dataloader = instruction_dataloader(training_params['micro_batch_size'], training_params['workers'], data_setting['eval'], 
                                                    prefix=data_path_prefix,
                                                    split='val',
                                                    config=dataloader_config,
                                                    custom_logger=custom_logger,
                                                    dataset_params=dataset_params)
    else:
        val_dataloader = None
    custom_logger.info("*"*100, color="red")
    trainer.train_dataloader = train_dataloader
    trainer.eval_dataloader = val_dataloader
    # ========================================== check if model already existed ===========================================
    custom_logger.info_w_delimiter("all is ready, start instruction training...", color='green')

    # set as default for debug on single gpu while model trained with deepspeed require same number of gpus for resume
    trainer.remove_callback(WandbCallback)
    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    custom_logger.info("Training completed.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base_config', type=str, default='src/config/instruction_tuning_local/base.yaml')
    parser.add_argument('--variant_config', type=str, default=None, help='variant config file, if not exist, use base config only') 
    parser.add_argument('--local_rank', type=int, default=-1)
    parser.add_argument('--deepspeed', type=str, default='src/config/deepspeed/deepspeed_config.json')
    args = parser.parse_args()
    main(args)
