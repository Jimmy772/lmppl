""" Calculate perplexity.
>>> scorer = LM()
>>> scores = scorer.get_perplexity(
    input_texts=['sentiment classification: I have a bad day is happy',
                 'sentiment classification: I have a bad day is sad'],
)
>>> print(scores)
[128.80070356559577, 100.5730992106926]
"""

import os
import logging
import gc
from math import exp
from typing import List
from tqdm import tqdm

import transformers
import torch

from .util import internet_connection

os.environ["OMP_NUM_THREADS"] = "1"  # to turn off warning message
os.environ["TOKENIZERS_PARALLELISM"] = "false"  # to turn off warning message
PAD_TOKEN_LABEL_ID = torch.nn.CrossEntropyLoss().ignore_index


class LM:
    """ Language Model. """
    def __init__(self,
                 model: str = 'gpt2',
                 use_auth_token: bool = False,
                 max_length: int = None,
                 num_gpus: int = None,
                 torch_dtype=None,
                 device_map: str = None,
                 low_cpu_mem_usage: bool = False,
                 trust_remote_code: bool = True,
                 offload_folder: str = None,
                 hf_cache_dir: str = None):
        """ Language Model.

        @param model: Model alias or path to local model file.
        @param use_auth_token: Huggingface transformers argument of `use_auth_token`
        @param device: Device name to load the models.
        @param num_gpus: Number of gpus to be used.
        """
        logging.info(f'Loading Model: `{model}`')

        # load model
        params = {
            "local_files_only": not internet_connection(),
            "use_auth_token": use_auth_token,
            "trust_remote_code": trust_remote_code
        }
        if hf_cache_dir is not None:
            params["cache_dir"] = hf_cache_dir
        if offload_folder is not None:
            params["offload_folder"] = offload_folder
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            model, **params)
        self.config = transformers.AutoConfig.from_pretrained(model, **params)

        params.update({
            "config": self.config,
            "low_cpu_mem_usage": low_cpu_mem_usage
        })
        if torch_dtype is not None:
            params['torch_dtype'] = torch_dtype
        if device_map is not None:
            params['device_map'] = device_map
        self.model = transformers.AutoModelForCausalLM.from_pretrained(
            model, **params)

        self.pad_token_initialized = False
        if self.tokenizer.pad_token is None:
            self.tokenizer.add_special_tokens({'pad_token': "<<PAD>>"})
            self.model.resize_token_embeddings(len(self.tokenizer))
            self.pad_token_initialized = True

        if max_length is None:
            self.max_length = None
        else:
            self.max_length = max_length if max_length is not None else self.tokenizer.model_max_length
            assert self.max_length <= self.tokenizer.model_max_length, f"{self.max_length} > {self.tokenizer.model_max_length}"

        # loss function
        self.loss_fct = torch.nn.CrossEntropyLoss(reduction='none')

        # GPU setup
        self.device = self.model.device
        if device_map is None:
            num_gpus = torch.cuda.device_count(
            ) if num_gpus is None else num_gpus
            if num_gpus == 1:
                self.model.to('cuda')
                self.device = self.model.device
            elif num_gpus > 1:
                self.model = torch.nn.DataParallel(self.model)
                self.model.to('cuda')
                self.device = self.model.module.device
        self.model.eval()
        logging.info(f'\t * model is loaded on: {self.device}')

    def get_perplexity(self, input_texts, batch=None, forced_reset=False):
        """ Compute the perplexity on recurrent LM.
        :param input_texts: A string or list of input texts for the encoder.
        :param batch: Batch size
        :param forced_reset: Whether to perform forced memory cleanup
        :return: A value or list of perplexity.
        """

        # batch preparation
        single_input = isinstance(input_texts, str)
        input_texts = [input_texts] if single_input else input_texts
        batch = len(input_texts) if batch is None else batch

        # Tokenize all input texts and create model inputs
        if self.max_length is not None:
            model_inputs = self.tokenizer(input_texts,
                                        max_length=self.max_length,
                                        truncation=True,
                                        padding='max_length',
                                        return_tensors='pt')
        else:
            model_inputs = self.tokenizer(input_texts,
                                        truncation=True,
                                        padding=True,
                                        return_tensors='pt')
        if 'token_type_ids' in model_inputs:
            model_inputs.pop('token_type_ids')

        loss_list = []
        with torch.no_grad():
            # Create a progress bar
            progress_bar = tqdm(range(0, len(input_texts), batch), unit='batch')
            for start in progress_bar:
                end = start + batch
                batch_inputs = {
                    k: v[start:end].to(self.device)
                    for k, v in model_inputs.items()
                }

                output = self.model(**batch_inputs)
                logit = output['logits']
                if self.pad_token_initialized:
                    logit = logit[:, :, :-1]

                # Shift the label sequence for causal inference
                label = batch_inputs['input_ids']
                label[label ==
                    self.tokenizer.pad_token_id] = PAD_TOKEN_LABEL_ID

                # Shift so that tokens < n predict n
                shift_logits = logit[..., :-1, :].contiguous()
                shift_label = label[:, 1:].contiguous()

                # Compute loss
                valid_length = (shift_label != PAD_TOKEN_LABEL_ID).sum(dim=-1)
                loss = self.loss_fct(
                    shift_logits.view(-1, shift_logits.size(-1)),
                    shift_label.view(-1))
                loss = loss.view(len(output['logits']), -1)
                loss = torch.sum(loss, -1) / valid_length
                loss_list += loss.cpu().tolist()

                if forced_reset:
                    del batch_inputs
                    del output
                    gc.collect()
                    torch.cuda.empty_cache()

        # Conversion to perplexity
        ppl = [exp(i) for i in loss_list]
        return ppl[0] if single_input else ppl


if __name__ == '__main__':

    # scorer = LM("gpt2")
    scorer = LM("facebook/opt-125m")
    text = [
        'sentiment classification: I dropped my laptop on my knee, and someone stole my coffee. I am happy.',
        'sentiment classification: I dropped my laptop on my knee, and someone stole my coffee. I am sad.'
    ]
    print(scorer.get_perplexity(text))
