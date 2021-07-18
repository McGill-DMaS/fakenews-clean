import random
from pathlib import Path
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchtext.data import dataset, RawField, Example, BucketIterator
from transformers import DistilBertTokenizer, DistilBertModel, DistilBertConfig, AdamW
from sklearn.metrics import f1_score, accuracy_score
from sklearn.model_selection import StratifiedKFold

# import getpass
# user = getpass.getuser()
# if user == 'Low':
#     import os
#     os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
#     os.environ["CUDA_VISIBLE_DEVICES"] = "1"
# else:
#     import os


torch.manual_seed(42)
random.seed(42)
np.random.seed(42)
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def get_weights(weight_name, weight_dir=None):
    weight_name_dict = dict(zip(
        ["simsents30k", "target30k", "external30k", "simsents7.5m", "target7.5m", "external7.5m"],
        ["maskedlm_golbeck_simsents_5k", "maskedlm_golbeck_5k", "maskedlm_nelagt_5k",
         "maskedlm_golbeck_simsents", "maskedlm_golbeck", "maskedlm_nelagt"]
    ))
    if weight_name == "distilbert-base-uncased":
        weight_path = weight_name
    else:
        assert weight_dir is not None
        weight_path = str(weight_dir / weight_name_dict[weight_name])
    return weight_path


class ModelTokenizer:
    def __init__(self, tokenizer_class, pretrained_weights):
        self.max_clm_len = 510
        self.max_seq_len = 512
        self.tokenizer = tokenizer_class.from_pretrained(pretrained_weights)

    def encode(self, clm):
        tokenizer = self.tokenizer
        cls_token = tokenizer.cls_token
        sep_token = tokenizer.sep_token
        clm_tkn = [cls_token] + tokenizer.tokenize(clm)[:self.max_clm_len] + [sep_token]
        clm_attn_mask = [1] * len(clm_tkn) + [0] * (self.max_seq_len - len(clm_tkn))
        clm_tkn = clm_tkn + [tokenizer.pad_token] * (self.max_seq_len - len(clm_tkn))
        clm_encoded = tokenizer.encode(clm_tkn, add_special_tokens=False)
        return clm_encoded, clm_attn_mask

    def encode_batch(self, batch_clm):
        batch_encoded = []
        batch_attn_mask = []
        for clm in batch_clm:
            clm_encoded, clm_attn_mask = self.encode(clm)
            batch_encoded.append(clm_encoded)
            batch_attn_mask.append(clm_attn_mask)
        batch_encoded = torch.as_tensor(batch_encoded).long()
        batch_attn_mask = torch.as_tensor(batch_attn_mask).long()
        return batch_encoded, batch_attn_mask


class SeqClassifier(nn.Module):
    def __init__(self, num_labels, dropout=0.3, rep_dim=768):
        super().__init__()
        self.rep_dim = rep_dim
        self.num_labels = num_labels
        self.seq_classif_dropout = dropout
        self.pre_classifier = nn.Linear(self.rep_dim, self.rep_dim)
        self.classifier = nn.Linear(self.rep_dim, self.num_labels)
        self.dropout = nn.Dropout(self.seq_classif_dropout)

    def forward(self, pooled_output):
        pooled_output = self.pre_classifier(pooled_output)
        pooled_output = nn.ReLU()(pooled_output)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)
        logits = F.log_softmax(logits, dim=1)
        return logits


class ClaimEvaluator(nn.Module):
    def __init__(self, num_labels, pretrained_weights="distilbert-base-uncased"):
        super().__init__()
        self.concat_dim = 768
        self.pretrained_weights = pretrained_weights
        self.num_labels = num_labels
        self.classifier = SeqClassifier(self.num_labels, rep_dim=self.concat_dim, dropout=0.1)
        self.bert_config = DistilBertConfig(dropout=0.1, attention_dropout=0.1)
        self.bert = DistilBertModel.from_pretrained(self.pretrained_weights, config=self.bert_config)

    def forward(self, clm, clm_attn):
        clm_pooled_output = self.bert(clm, clm_attn)[0][:, 0]
        logits = self.classifier(clm_pooled_output)
        return logits


n_epochs = 10
n_classes = 2
batch_size_ = 8
learn_rate_all = 1e-5
learn_rate_finetune = 2e-5
checkpoints_per_epoch = 1
kfold_ = 5
text_type = "body"  # "title", "body"

pretrained_weights_name = "external7.5m"  # ["simsents", "target", "external"] * ["7.5m", "30k"], "distilbert-base-uncased"
model_params_dir = Path('K:/Work/ModelParams').expanduser()

horne_dir = Path("K:/Work/Datasets-FakeNews/source-reliability/Horne2017_FakeNewsData/"
                 "Public Data/Random Poltical News Dataset")
fakes_df_path = horne_dir / "fakes_df.tsv"
satires_df_path = horne_dir / "satires_df.tsv"
fakes_df = pd.read_csv(fakes_df_path, sep="\t").fillna("")
satires_df = pd.read_csv(satires_df_path, sep="\t").fillna("")

fake_satire = (
    list(zip([1]*len(fakes_df), fakes_df["title"].values, fakes_df["body"].values)) +
    list(zip([0]*len(satires_df), satires_df["title"].values, satires_df["body"].values)))
random.shuffle(fake_satire)

fake_satire_X = [row[1] for row in fake_satire]
fake_satire_y = [row[0] for row in fake_satire]
skf = StratifiedKFold(n_splits=kfold_)
skf_splits = skf.split(fake_satire_X, fake_satire_y)

valid_metrics = []
for train_ix, valid_ix in skf_splits:
    print("")
    train_pairs = [fake_satire[ix] for ix in train_ix]
    valid_pairs = [fake_satire[ix] for ix in valid_ix]

    id_field = RawField()
    label_field = RawField()
    title_field = RawField()
    text_field = RawField()
    article_field = RawField()
    claim_fields = [('label', label_field), ('title', title_field), ('body', text_field)]
    train_examples = [Example.fromlist(row, claim_fields) for row in train_pairs]
    valid_examples = [Example.fromlist(row, claim_fields) for row in valid_pairs]
    train_dataset = dataset.Dataset(train_examples, claim_fields)
    valid_dataset = dataset.Dataset(valid_examples, claim_fields)

    model_trfmr_weights = get_weights(pretrained_weights_name, model_params_dir)
    tokenizer_class_ = DistilBertTokenizer
    tokenizer_weights_ = 'distilbert-base-uncased'
    model_tokenizer = ModelTokenizer(tokenizer_class_, tokenizer_weights_)
    claims_model = ClaimEvaluator(num_labels=n_classes, pretrained_weights=model_trfmr_weights)
    claims_model.to(device)

    criterion = nn.NLLLoss().to(device)
    optimizer = AdamW([
        {"params": claims_model.classifier.parameters()},
        {"params": claims_model.bert.parameters(),
         "lr": learn_rate_finetune,
         "weight_decay": 1e-2}],
        lr=learn_rate_all,
        weight_decay=1e-3)
    valid_f1_hiscore = 0
    for i in range(n_epochs):
        train_pred_list = []
        train_tgt_list = []
        train_epoch_loss = 0
        print("Epoch " + str(i+1))
        train_iterator = BucketIterator(train_dataset, batch_size=batch_size_, shuffle=True)
        valid_iterator = BucketIterator(valid_dataset, batch_size=batch_size_, shuffle=False)
        for step, train_batch in enumerate(train_iterator):
            claims_model.train()
            optimizer.zero_grad()
            label_ = torch.as_tensor(train_batch.label).long().to(device)
            title_ = train_batch.title
            body_ = train_batch.body
            if text_type == "title":
                text_to_prepare = title_
            elif text_type == "body":
                text_to_prepare = body_
            text_ = text_to_prepare
            batch_encoded_, batch_attn_mask_ = model_tokenizer.encode_batch(text_)
            batch_encoded_ = torch.as_tensor(batch_encoded_).to(device)
            batch_attn_mask_ = torch.as_tensor(batch_attn_mask_).to(device)
            train_pred = claims_model(batch_encoded_, batch_attn_mask_)
            loss = criterion(train_pred, label_)
            loss.backward()
            optimizer.step()
            train_epoch_loss += loss.item()
            train_pred_list.append(train_pred.detach().cpu().numpy())
            train_tgt_list.append(train_batch.label)
            if (step + 1) % round(len(train_iterator) / checkpoints_per_epoch) == 0 or (step + 1) == len(train_iterator):
                train_pred_scoring = np.argmax(np.vstack(train_pred_list), axis=1)
                train_tgt_scoring = np.hstack(train_tgt_list)
                train_f1 = f1_score(train_tgt_scoring, train_pred_scoring,
                                    labels=list(range(n_classes)), average='weighted')
                print("Training F1: " + str(train_f1.round(4)) + ", " +
                      "epoch loss: " + str(round(train_epoch_loss / (step+1), 4)))
                with torch.no_grad():
                    claims_model.eval()
                    valid_pred_list = []
                    valid_tgt_list = []
                    valid_epoch_loss = 0
                    for valid_batch in valid_iterator:
                        label_ = torch.as_tensor(valid_batch.label).long().to(device)
                        title_ = valid_batch.title
                        body_ = valid_batch.body
                        if text_type == "title":
                            text_to_prepare = title_
                        elif text_type == "body":
                            text_to_prepare = body_
                        text_ = text_to_prepare
                        batch_encoded_, batch_attn_mask_ = model_tokenizer.encode_batch(text_)
                        batch_encoded_ = torch.as_tensor(batch_encoded_).to(device)
                        batch_attn_mask_ = torch.as_tensor(batch_attn_mask_).to(device)
                        valid_pred = claims_model(batch_encoded_, batch_attn_mask_)
                        loss = criterion(valid_pred, label_)
                        valid_epoch_loss += loss.item()
                        valid_pred_list.append(valid_pred.detach().cpu().numpy())
                        valid_tgt_list.append(valid_batch.label)
                    valid_pred_flat = [pred for sublist in valid_pred_list for pred in sublist]
                    valid_pred_scoring = np.argmax(np.vstack(valid_pred_list), axis=1)
                    valid_tgt_scoring = np.hstack(valid_tgt_list)
                    valid_f1 = f1_score(valid_tgt_scoring, valid_pred_scoring,
                                        labels=list(range(n_classes)), average='weighted')
                    valid_acc = accuracy_score(valid_tgt_scoring, valid_pred_scoring,)
                    valid_loss = valid_epoch_loss / len(valid_iterator)
                    print("Validation F1: " + str(valid_f1.round(4)) + ", " +
                          "Acc: " + str(valid_acc.round(4)) + ", " +
                          "epoch loss: " + str(round(valid_loss, 4)))
                    if valid_f1 > valid_f1_hiscore:
                        valid_f1_hiscore = valid_f1
    valid_metrics.append([valid_f1, valid_acc, valid_loss])

print("\n".join(["\t".join([str(x) for x in row]) for row in valid_metrics]))
print("\t".join([str(x) for x in np.array(valid_metrics).mean(axis=0)]))
print("\t".join([str(x) for x in np.array(valid_metrics).std(axis=0)]))
