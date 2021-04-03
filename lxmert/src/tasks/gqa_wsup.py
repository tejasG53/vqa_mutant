# coding=utf-8
# Copyleft 2019 project LXRT.

import os
import collections

import torch
from tqdm import tqdm
import torch.nn as nn
from torch.utils.data.dataloader import DataLoader

from param import args
from pretrain.qa_answer_table import load_lxmert_qa
from tasks.gqa_model_wsup import GQAModel
from tasks.gqa_data_wsup import GQADataset, GQATorchDataset, GQAEvaluator
from tasks.vqa_data_wsup import VQADataset, VQATorchDataset, VQAEvaluator


DataTuple = collections.namedtuple("DataTuple", 'dataset loader evaluator')


def get_tuple(splits: str, bs:int, shuffle=False, drop_last=False, vqa=False) -> DataTuple:
    dset = GQADataset(splits)
    tset = GQATorchDataset(dset)
    evaluator = GQAEvaluator(dset)
    
    data_loader = DataLoader(
        tset, batch_size=bs,
        shuffle=shuffle, num_workers=0,
        drop_last=drop_last, pin_memory=True
    )

    return DataTuple(dataset=dset, loader=data_loader, evaluator=evaluator)


class GQA:
    def __init__(self):
        self.train_tuple = get_tuple(
            args.train, bs=args.batch_size, shuffle=True, drop_last=True, vqa=True
        )
        if args.valid != "":
            valid_bsize = 2048 if args.multiGPU else 512
            self.valid_tuple = get_tuple(
                args.valid, bs=valid_bsize,
                shuffle=False, drop_last=False, vqa=False
            )
        else:
            self.valid_tuple = None

#         self.model = GQAModel(self.train_tuple.dataset.num_answers)
        self.model = GQAModel(17175)

        # Load pre-trained weights
        if args.load_lxmert is not None:
            self.model.lxrt_encoder.load(args.load_lxmert)
        if args.load_lxmert_qa is not None:
            load_lxmert_qa(args.load_lxmert_qa, self.model,
                           label2ans=self.train_tuple.dataset.label2ans)

        # GPU options
        self.model = self.model.cuda()
        if args.multiGPU:
            self.model.lxrt_encoder.multi_gpu()

        # Losses and optimizer
        self.bce_loss = nn.BCEWithLogitsLoss()
        self.mce_loss = nn.CrossEntropyLoss(ignore_index=-1)
        self.ce_loss = nn.CrossEntropyLoss()
        if 'bert' in args.optim:
            batch_per_epoch = len(self.train_tuple.loader)
            t_total = int(batch_per_epoch * args.epochs)
            print("Total Iters: %d" % t_total)
            from lxrt.optimization import BertAdam
            self.optim = BertAdam(list(self.model.parameters()),
                                  lr=args.lr,
                                  warmup=0.1,
                                  t_total=t_total)
        else:
            self.optim = args.optimizer(list(self.model.parameters()), args.lr)

        self.output = args.output
        os.makedirs(self.output, exist_ok=True)

    def train(self, train_tuple, eval_tuple):
        dset, loader, evaluator = train_tuple
        total_len = len(loader)
        eval_itx = int(0.1*total_len)
        
        iter_wrapper = (lambda x: tqdm(x, total=total_len)) if args.tqdm else (lambda x: x)

        best_valid = 0.
        total_corr = 0.
        total_elem = 0.
        total_bz = 0.
        total_loss = 0.
        total_wkloss = 0.
        for epoch in range(args.epochs):
            quesid2ans = {}
            for i, (ques_id, feats, boxes, sent, target, sp_target) in iter_wrapper(enumerate(loader)):

                self.model.train()
                self.optim.zero_grad()

                feats, boxes, target = feats.cuda(), boxes.cuda(), target.cuda()
                
                sp_target = sp_target.cuda()
                
                logit, v_logits = self.model(feats, boxes, sent)
                assert logit.dim() == target.dim() == 2
                
#                 print(sp_target.shape,v_logits.shape)
                sp_target = sp_target.view([sp_target.shape[0]*36*36*3])
                v_logits = v_logits.view([v_logits.shape[0]*3*36*36,3])
#                 print(sp_target.shape,v_logits.shape)

                corrs = (v_logits.argmax(-1) == sp_target).float()
                total_corr += corrs.sum().cpu().detach().numpy()
                total_elem += corrs.shape[0]
                
                if args.mce_loss:
                    max_value, target = target.max(1)
                    loss = self.mce_loss(logit, target) * logit.size(1)
                else:
                    loss = self.bce_loss(logit, target)
                    loss = loss * logit.size(1)
                    
                
                weak_loss = self.ce_loss(v_logits,sp_target) * v_logits.size(1)
#                 print(weak_loss)

                
                loss = (loss + weak_loss)/2
            
            
                loss.backward()
                
                total_wkloss += weak_loss.cpu().detach().numpy()
                total_loss += loss.cpu().detach().numpy()
                
                
                nn.utils.clip_grad_norm_(self.model.parameters(), 5.)
                self.optim.step()

                score, label = logit.max(1)
                total_bz += label.shape[0]
                
                for qid, l in zip(ques_id, label.cpu().numpy()):
                    ans = dset.label2ans[l]
                    quesid2ans[qid] = ans

            

                if self.valid_tuple is not None and (i%eval_itx==0 or i==total_len-1):  # Do Validation

                    log_str = "\nEpoch %d: Train %0.2f\n" % (epoch, evaluator.evaluate(quesid2ans) * 100.)
                    log_str += "\nSpatial Train Acc: %0.3f" % ((total_corr/total_elem)*100.)
                    log_str += "\nTrain Loss: %0.3f" % ((loss/total_bz))
                    log_str += "\nTrain Spatial Loss: %0.3f\n" % ((total_wkloss/total_bz))

                    valid_score = self.evaluate(eval_tuple)
                    if valid_score > best_valid:
                        best_valid = valid_score
                        self.save("BEST")

                    log_str += "Epoch %d: Valid %0.2f\n" % (epoch, valid_score * 100.) + \
                               "Epoch %d: Best %0.2f\n" % (epoch, best_valid * 100.)

                    print(log_str, end='', flush=True)

                    with open(self.output + "/log.log", 'a') as f:
                        f.write(log_str)
                        f.flush()

        self.save("LAST")

    def predict(self, eval_tuple: DataTuple, dump=None):
        self.model.eval()
        dset, loader, evaluator = eval_tuple
        quesid2ans = {}
        for i, datum_tuple in tqdm(enumerate(loader),total=len(loader)):
            ques_id, feats, boxes, sent = datum_tuple[:4]   # avoid handling target
            with torch.no_grad():
                feats, boxes = feats.cuda(), boxes.cuda()
                logit, v_logits = self.model(feats, boxes, sent)
                score, label = logit.max(1)
                for qid, l in zip(ques_id, label.cpu().numpy()):
                    ans = dset.label2ans[l]
                    quesid2ans[qid] = ans
        if dump is not None:
            evaluator.dump_result(quesid2ans, dump)
        return quesid2ans

    def evaluate(self, eval_tuple: DataTuple, dump=None):
        dset, loader, evaluator = eval_tuple
        quesid2ans = self.predict(eval_tuple, dump)
        return evaluator.evaluate(quesid2ans)

    @staticmethod
    def oracle_score(data_tuple):
        dset, loader, evaluator = data_tuple
        quesid2ans = {}
        for i, (ques_id, feats, boxes, sent, target, sp_target) in enumerate(loader):
            _, label = target.max(1)
            for qid, l in zip(ques_id, label.cpu().numpy()):
#                 print(qid,l)
                ans = dset.label2ans[l]
                quesid2ans[qid] = ans
        return evaluator.evaluate(quesid2ans)

    def save(self, name):
        torch.save(self.model.state_dict(),
                   os.path.join(self.output, "%s.pth" % name))

    def load(self, path):
        print("Load model from %s" % path)
        state_dict = torch.load("%s.pth" % path)
        for key in list(state_dict.keys()):
            if '.module' in key:
                state_dict[key.replace('.module', '')] = state_dict.pop(key)
        self.model.load_state_dict(state_dict, strict=False)


if __name__ == "__main__":
    # Build Class
    gqa = GQA()

    # Load Model
    if args.load is not None:
        gqa.load(args.load)

    # Test or Train
    if args.test is not None:
        args.fast = args.tiny = False       # Always loading all data in test
        if 'submit' in args.test:
            gqa.predict(
                get_tuple(args.test, bs=args.batch_size,
                          shuffle=False, drop_last=False),
                dump=os.path.join(args.output, 'submit_predict.json')
            )
        if 'testdev' in args.test:
            result = gqa.evaluate(
                get_tuple('testdev', bs=args.batch_size,
                          shuffle=False, drop_last=False),
                dump=os.path.join(args.output, 'testdev_predict.json')
            )
            print(result)
    else:
        # print("Train Oracle: %0.2f" % (gqa.oracle_score(gqa.train_tuple) * 100))
        print('Splits in Train data:', gqa.train_tuple.dataset.splits)
        if gqa.valid_tuple is not None:
            print('Splits in Valid data:', gqa.valid_tuple.dataset.splits)
            print("Valid Oracle: %0.2f" % (gqa.oracle_score(gqa.valid_tuple) * 100))
        else:
            print("DO NOT USE VALIDATION")
        gqa.train(gqa.train_tuple, gqa.valid_tuple)

