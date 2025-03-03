import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

import transformers
from transformers import AdamW, get_linear_schedule_with_warmup

from sklearn import model_selection, metrics

from tqdm import tqdm
from collections import defaultdict

import warnings
warnings.filterwarnings('ignore')

# Load the original dataset
df = pd.read_csv('missom_annotated.csv')
df = df.rename(columns={'label_minority_stress': 'label'})
df = df[['label', 'text']]

empty_cells = df.isnull().sum()
print(empty_cells)
df.dropna(inplace=True)

empty_cells = df.isnull().sum()
print(empty_cells)

# Split the dataset into train, validation, and test sets
train_df, temp_df = model_selection.train_test_split(df, test_size=0.3, random_state=42, stratify=df['label'])
valid_df, test_df = model_selection.train_test_split(temp_df, test_size=0.5, random_state=42, stratify=temp_df['label'])

# Print lengths of datasets
print(f"Length of train dataset: {len(train_df)}")
print(f"Length of validation dataset: {len(valid_df)}")
print(f"Length of test dataset: {len(test_df)}")

train_df.to_csv('train.csv', index=False)
valid_df.to_csv('valid.csv', index=False)
test_df.to_csv('test.csv', index=False)

class Config:
    MAX_LEN = 512
    TRAIN_BATCH_SIZE = 4
    VALID_BATCH_SIZE = 4
    EPOCHS = 4
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    BERT_PATH = "roberta-base"
    MODEL_PATH = "RoBERTaModel.bin"
    TRAINING_DATA = "train.csv"
    VALIDATION_DATA = "valid.csv"
    TEST_DATA = "test.csv"
    TOKENIZER = transformers.RobertaTokenizer.from_pretrained(BERT_PATH, do_lower_case=True)
    MODEL = transformers.RobertaModel.from_pretrained(BERT_PATH, return_dict=False)


class MinorityStressDataset(Dataset):
    def __init__(self, data_path):
        self.data = pd.read_csv(data_path).fillna('none')
        self.data = self.data.reset_index(drop=True)
        self.tokenizer = Config.TOKENIZER
        self.max_len = Config.MAX_LEN
        self.text = self.data.text.values
        self.label = self.data.label.values

    def __len__(self):
        return len(self.text)

    def __getitem__(self, item):
        text = str(self.text[item])
        text = " ".join(text.split())

        inputs = self.tokenizer.encode_plus(
            text,
            None,
            add_special_tokens=True,
            max_length=self.max_len,
            padding="max_length",
            truncation=True
        )

        input_ids = inputs["input_ids"]
        attention_mask = inputs["attention_mask"]

        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(self.label[item], dtype=torch.long)
        }


class RoBERTaModel(nn.Module):
    def __init__(self):
        super(RoBERTaModel, self).__init__()
        self.roberta = Config.MODEL
        self.drop = nn.Dropout(0.3)
        self.out = nn.Linear(768, 2)

    def forward(self, input_ids, attention_mask):
        outputs = self.roberta(input_ids=input_ids, attention_mask=attention_mask)
        mean_last_hidden_state = torch.mean(outputs[0], dim=1)
        dropped_out = self.drop(mean_last_hidden_state)
        output = self.out(dropped_out)
        return output
def loss_fn(outputs, labels):
    return nn.CrossEntropyLoss()(outputs, labels)


# ROC Curve saving function
def save_roc_curve(y_true, test_probs, filename):
    fpr, tpr, thresholds = metrics.roc_curve(y_true, test_probs)
    auc_score = metrics.roc_auc_score(y_true, test_probs)
    data = np.column_stack((fpr, tpr, thresholds))
    header = "False Positive Rate (FPR)\tTrue Positive Rate (TPR)\tThresholds"
    np.savetxt(filename + '.txt', data, header=header, delimiter='\t')
    plt.figure()
    plt.plot(fpr, tpr, color='blue', lw=2, label='ROC curve (area = %0.4f)' % auc_score)
    plt.plot([0, 1], [0, 1], color='grey', lw=2, linestyle='--')
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.title('Receiver Operating Characteristic')
    plt.legend(loc="lower right")
    plt.savefig(filename + '.png')
    plt.close()


def train_fn(train_dataloader, model, optimizer, device, scheduler):
    model.train()
    final_loss = 0

    fin_labels = []
    fin_outputs = []
    for bi, d in tqdm(enumerate(train_dataloader), total=len(train_dataloader)):
        input_ids = d["input_ids"]
        attention_mask = d["attention_mask"]
        labels = d["labels"]

        input_ids = input_ids.to(device, dtype=torch.long)
        attention_mask = attention_mask.to(device, dtype=torch.long)
        labels = labels.to(device, dtype=torch.long)

        optimizer.zero_grad()
        outputs = model(input_ids=input_ids, attention_mask=attention_mask)
        loss = loss_fn(outputs, labels)

        final_loss += loss.item()
        fin_labels.extend(labels.cpu().detach().numpy().tolist())
        fin_outputs.extend(torch.softmax(outputs, dim=1).cpu().detach().numpy().tolist())

        loss.backward()
        optimizer.step()
        scheduler.step()

    return fin_outputs, fin_labels, final_loss / len(train_dataloader)


def eval_fn(valid_dataloader, model, device):
    model.eval()
    final_loss = 0

    fin_labels = []
    fin_outputs = []
    with torch.no_grad():
        for bi, d in tqdm(enumerate(valid_dataloader), total=len(valid_dataloader)):
            input_ids = d["input_ids"]
            attention_mask = d["attention_mask"]
            labels = d["labels"]

            input_ids = input_ids.to(device, dtype=torch.long)
            attention_mask = attention_mask.to(device, dtype=torch.long)
            labels = labels.to(device, dtype=torch.long)

            outputs = model(input_ids=input_ids, attention_mask=attention_mask)

            loss = loss_fn(outputs, labels)
            final_loss += loss.item()

            fin_labels.extend(labels.cpu().detach().numpy().tolist())
            fin_outputs.extend(torch.softmax(outputs, dim=1).cpu().detach().numpy().tolist())

    save_roc_curve(fin_labels, [prob[0] for prob in fin_outputs], 'ms_roc_curve_roberta')
    return fin_outputs, fin_labels, final_loss / len(valid_dataloader)


# Initialize datasets and dataloaders
train_data = MinorityStressDataset(Config.TRAINING_DATA)
valid_data = MinorityStressDataset(Config.VALIDATION_DATA)
test_data = MinorityStressDataset(Config.TEST_DATA)

train_dataloader = DataLoader(train_data, batch_size=Config.TRAIN_BATCH_SIZE, num_workers=4)
valid_dataloader = DataLoader(valid_data, batch_size=Config.VALID_BATCH_SIZE, num_workers=1)
test_dataloader = DataLoader(test_data, batch_size=Config.VALID_BATCH_SIZE, num_workers=1)

model = RoBERTaModel()
model = nn.DataParallel(model)
model.to(Config.DEVICE)

param_optimizer = list(model.named_parameters())
no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
optimizer_parameters = [
    {
        "params": [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
        "weight_decay": 0.001,
    },
    {
        "params": [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
        "weight_decay": 0.0,
    },
]

num_train_steps = int(len(train_data) / Config.TRAIN_BATCH_SIZE * Config.EPOCHS)
optimizer = AdamW(optimizer_parameters, lr=3e-5)
scheduler = get_linear_schedule_with_warmup(
    optimizer, num_warmup_steps=0, num_training_steps=num_train_steps
)

history = defaultdict(list)

best_accuracy = 0
for epoch in range(1, Config.EPOCHS + 1):
    train_outputs, train_labels, train_loss = train_fn(train_dataloader, model, optimizer, Config.DEVICE, scheduler)
    valid_outputs, valid_labels, valid_loss = eval_fn(valid_dataloader, model, Config.DEVICE)

    train_outputs = np.array(train_outputs)[:, 1] >= 0.5
    valid_outputs = np.array(valid_outputs)[:, 1] >= 0.5

    train_accuracy = metrics.accuracy_score(train_labels, train_outputs)
    valid_accuracy = metrics.accuracy_score(valid_labels, valid_outputs)

    print(f"Epoch: {epoch}\nTrain Loss: {train_loss} - Train Accuracy: {train_accuracy} \nValid Loss: {valid_loss} - Valid Accuracy: {valid_accuracy}\n")

    history['Train Loss'].append(train_loss)
    history['Train Accuracy'].append(train_accuracy)
    history['Valid Loss'].append(valid_loss)
    history['Valid Accuracy'].append(valid_accuracy)

    if valid_accuracy > best_accuracy:
        torch.save(model.state_dict(), Config.MODEL_PATH)
        best_accuracy = valid_accuracy

# Evaluate on the test set
test_outputs, test_labels, _ = eval_fn(test_dataloader, model, Config.DEVICE)
test_outputs = np.array(test_outputs)[:, 1] >= 0.5

# Generate and print the classification report
classification_report_text = metrics.classification_report(test_labels, test_outputs, digits=4)
print("==="*50)
print("\nClassification report \n\n", classification_report_text)
print("==="*50)

# Save the classification report to a text file
with open("classification_report.txt", "w") as file:
    file.write(classification_report_text)

print("=" * 150)
cm = metrics.confusion_matrix(test_labels, test_outputs)
fig, ax = plt.subplots()
sns.heatmap(cm, annot=True, fmt='d', ax=ax, cmap=plt.cm.Blues, cbar=False)
ax.set(xlabel="Predicted Label",
       ylabel="True Label",
       xticklabels=np.unique(test_labels),
       yticklabels=np.unique(test_labels),
       title="CONFUSION MATRIX")
plt.yticks(rotation=0)

# Save the confusion matrix plot
plt.savefig('ms_roberta_confusion_matrix.png')
plt.close()

# Save the training history plots
plt.figure()
plt.plot(history['Train Accuracy'], '-o', label='Train Accuracy')
plt.plot(history['Valid Accuracy'], '-o', label='Validation Accuracy')
plt.title('Training History - Accuracy')
plt.ylabel('Accuracy')
plt.xlabel('Epoch')
plt.legend()
plt.ylim([0, 1])
plt.savefig('ms_roberta_accuracy_history.png')
plt.close()

plt.figure()
plt.plot(history['Train Loss'], '-o', label='Train Loss')
plt.plot(history['Valid Loss'], '-o', label='Validation Loss')
plt.title('Training History - Loss')
plt.ylabel('Loss')
plt.xlabel('Epoch')
plt.legend()
plt.ylim([0, 1])
plt.savefig('ms_roberta_loss_history.png')
plt.close()