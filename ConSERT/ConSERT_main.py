"""
The system trains BERT (or any other transformer model like RoBERTa, DistilBERT etc.) on the SNLI + MultiNLI (AllNLI) dataset
with softmax loss function. At every 1000 training steps, the model is evaluated on the
STS benchmark dataset

Usage:
python training_nli.py --seed 1234

OR
python training_nli.py --seed 1234 --model_name_or_path bert-base-uncased
"""
from ast import arg
from torch.utils.data import DataLoader
import math
from sentence_transformers import models, losses
from sentence_transformers import SentencesDataset, LoggingHandler, SentenceTransformer, util, InputExample
from sentence_transformers.evaluation import EmbeddingSimilarityEvaluator, SimilarityFunction
import logging
from datetime import datetime
import sys
import os
import json
import copy
import gzip
import csv
import random
import torch
import numpy as np
import argparse
import shutil

from tensorboardX import SummaryWriter
from eval import eval_nli_unsup, eval_chinese_unsup
from data_utils import load_datasets, save_samples, load_senteval_binary, load_senteval_sst, load_senteval_trec, load_senteval_mrpc, load_chinese_tsv_data
from correlation_visualization import corr_visualization




def set_seed(seed: int, for_multi_gpu: bool):
    """
    Added script to set random seed.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if for_multi_gpu:
        torch.cuda.manual_seed_all(seed)





def main():

    parser = argparse.ArgumentParser()
    #어떤 방식으로 training할건지 받는 인자만들기
    parser.add_argument("--no_pair", action="store_true", help="If provided, do not pair two training texts") #일단 냅두자
    parser.add_argument("--data_proportion", type=float, default=1.0, help="The proportion of training dataset")
    parser.add_argument("--do_upsampling", action="store_true", help="If provided, do upsampling to original size of training dataset")
    parser.add_argument("--seed", type=int, required=True, help="Random seed for reproducing experimental results")
    parser.add_argument("--model_name_or_path", type=str, default="bert-base-uncased", help="The model path or model name of pre-trained model")
    parser.add_argument("--continue_training", action="store_true", help="Whether to continue training or just from BERT")
    parser.add_argument("--model_save_path", type=str, default=None, help="Custom output dir")
    parser.add_argument("--tensorboard_log_dir", type=str, default=None, help="Custom tensorboard log dir")
    parser.add_argument("--force_del", action="store_true", help="Delete the existing save_path and do not report an error")
    
    parser.add_argument("--use_apex_amp", action="store_true", help="Use apex amp or not")
    parser.add_argument("--apex_amp_opt_level", type=str, default=None, help="The opt_level argument in apex amp")
    
    parser.add_argument("--batch_size", type=int, default=16, help="Training mini-batch size")
    parser.add_argument("--num_epochs", type=int, default=1, help="Number of training epochs")
    parser.add_argument("--learning_rate", type=float, default=2e-5, help="The learning rate")
    parser.add_argument("--evaluation_steps", type=int, default=1000, help="The steps between every evaluations")
    parser.add_argument("--max_seq_length", type=int, default=128, help="The max sequence length")
    parser.add_argument("--loss_rate_scheduler", type=int, default=0, help="The loss rate scheduler, default strategy 0 (i.e. do nothing, see AdvCLSoftmaxLoss for more details)")
    parser.add_argument("--no_dropout", action="store_true", help="Add no dropout when training")
    
    parser.add_argument("--concatenation_sent_max_square", action="store_true", help="Concat max-square features of two text representations when training classification")
    parser.add_argument("--normal_loss_stop_grad", action="store_true", help="Use stop gradient to normal loss or not")

    #나중에 구현해야할지도??, 논문에서도 언급만되어있지 실험결과 없음
    parser.add_argument("--adv_training", action="store_true", help="Use adversarial training or not") 
    parser.add_argument("--adv_loss_rate", type=float, default=1.0, help="The adversarial loss rate")
    parser.add_argument("--noise_norm", type=float, default=1.0, help="The perturbation norm")
    parser.add_argument("--adv_loss_stop_grad", action="store_true", help="Use stop gradient to adversarial loss or not")

    parser.add_argument("--add_cl", action="store_true", help="Use contrastive loss or not")
    parser.add_argument("--data_augmentation_strategy", type=str, default="adv", choices=["adv", "none", "meanmax", "shuffle", "cutoff", "shuffle-cutoff", "shuffle+cutoff", "shuffle_embeddings"], help="The data augmentation strategy in contrastive learning")
    
    #feature cutoff는 무조건 column자르는거여서 정작 이거 필요없는데
    parser.add_argument("--cutoff_direction", type=str, default=None, help="The direction of cutoff strategy, row, column or random")
    parser.add_argument("--cutoff_rate", type=float, default=None, help="The rate of cutoff strategy, in (0.0, 1.0)")
    parser.add_argument("--cl_loss_only", action="store_true", help="Ignore the main task loss (e.g. the CrossEntropy loss) and use the contrastive loss only")
    parser.add_argument("--cl_rate", type=float, default=0.01, help="The contrastive loss rate")
    parser.add_argument("--regularization_term_rate", type=float, default=0.0, help="The loss rate of regularization term for contrastive learning") 
    parser.add_argument("--cl_type", type=str, default="nt_xent", help="The contrastive loss type, nt_xent or cosine")
    parser.add_argument("--temperature", type=float, default=0.5, help="The temperature for contrastive loss")
    parser.add_argument("--mapping_to_small_space", type=int, default=None, help="Whether to mapping sentence representations to a low dimension space (similar to SimCLR) and give the dimension")
    parser.add_argument("--add_contrastive_predictor", type=str, default=None, help="Whether to use a predictor on one side (similar to SimSiam) and give the projection added to which side (normal or adv)")
    parser.add_argument("--add_projection", action="store_true", help="Add projection layer before predictor, only be considered when add_contrastive_predictor is not None")
    parser.add_argument("--projection_norm_type", type=str, default=None, help="The norm type used in the projection layer beforn predictor")
    parser.add_argument("--projection_hidden_dim", type=int, default=None, help="The hidden dimension of the projection or predictor MLP")
    parser.add_argument("--projection_use_batch_norm", action="store_true", help="Whether to use batch normalization in the hidden layer of MLP")
    parser.add_argument("--contrastive_loss_stop_grad", type=str, default=None, help="Use stop gradient to contrastive loss (and which mode to apply) or not")
    
    parser.add_argument("--da_final_1", type=str, default=None, help="The final 5 data augmentation strategies for view1 (none, shuffle, token_cutoff, feature_cutoff, dropout, span)")
    parser.add_argument("--da_final_2", type=str, default=None, help="The final 5 data augmentation strategies for view2 (none, shuffle, token_cutoff, feature_cutoff, dropout, span)")
    parser.add_argument("--cutoff_rate_final_1", type=float, default=None, help="The final cutoff/dropout rate for view1")
    parser.add_argument("--cutoff_rate_final_2", type=float, default=None, help="The final cutoff/dropout rate for view2")
    parser.add_argument("--patience", default=None, type=int, help="The patience for early stop")

    ################# ADDED #################
    parser.add_argument('--gpu', default=0, type=int) 
    parser.add_argument('--task', default='unsup', type=str)

    args = parser.parse_args()
    setattr(args, 'device', f'cuda:{args.gpu}' if torch.cuda.is_available() and args.gpu >= 0 else 'cpu')
    setattr(args, 'time', datetime.datetime.now().strftime('%Y%m%d-%H:%M:%S'))
    ################# ADDED #################

    
    if args.task == 'unsup':
        #load_dataset
        #+ loss function까지??,,  그냥 model에다가 unsup signal넘기고 model에서 처리하는게 낫을까?
        pass
    elif args.task == 'joint':
        #to do
        pass
    elif args.task == 'sup-unsup':
        #to do
        pass
    elif args.task == 'joint-unsup':
        #to do
        pass
    
    #transformer와 pooling을 nn.sequential 클래스로 묶는게 편할까?


if __name__ == "__main__":
    main()


        