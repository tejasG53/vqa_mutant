# VQA-MUTANT 

Code for MUTANT: A Training Paradigm for Out-of-Distribution Generalization in Visual Question Answering
Tejas Gokhale, Pratyay Banerjee, Chitta Baral, Yezhou Yang
EMNLP 2020 
https://arxiv.org/abs/2009.08566

In this paper, we use two backbone models: LXMERT (Tan et al. EMNLP 2019) and UpDn (Anderson et al. CVPR 2017).
The instructions for runnning each can be found in respective folders. 

## For LXMERT, 
`cd lxmert`

### Download data and pre-trained model
(features and json files for mutant samples) 
- download image features from: 
- put them under `data/mutant_imgfeat/`
- download the .json files from: 
- Put the .json files under `data/vqa/`
- download pre-trained model from: 

### Command to test pre-trained model
### Command to run training:
```
bash run/vqa_finetune_mutant.bash 0 debug
```
where 0 is the gpu-id and debug is the name of the experiment (these are command line arguments that can be changed according to your preference.)

