import torchtext
from torchtext import data
from torchtext import datasets
from torchtext.vocab import FastText

import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch
from torch.autograd import Variable

import unicodedata
import string


import dill

from itertools import chain

torch.manual_seed(0)
torch.cuda.manual_seed(0)


# torchtextによる前処理

# text用のTEXTフィールドとラベル用のLABELフィールドを用意
TEXT = data.Field(sequential=True,lower=True,include_lengths=True, batch_first=True)
LABEL = data.Field(sequential=False, use_vocab=False)

# データパスと各カラムに対応するFieldを指定してデータを読み込む
train, test = data.TabularDataset.splits(
    path='./', train='train.csv',
    test='test.csv', format='csv',
    fields=[('Text', TEXT), ('Label', LABEL)])

print('len(train)', len(train))

print('vars(train[0])', vars(train[0]))

# 単語へ番号を振る,また学習済みの単語ベクトルを指定し読み込む
TEXT.build_vocab(train, vectors=FastText(language="ja"))


# TEXT.build_vocab(train, vectors=GloVe(name='6B', dim=args.emb_dim))
# LABEL.build_vocab(train)

# save data field
dill.dump(TEXT, open("TEXT.pkl",'wb'))
dill.dump(LABEL, open("LABEL.pkl",'wb'))

# 各単語を番号に変換してミニバッチごとにまとめた行列を返すイテレータを作成
train_iter, test_iter = data.Iterator.splits(
    (train, test), batch_sizes=(2, 2), device=-1,repeat=False,sort=False)

# イテレータが返す結果
batch = next(iter(train_iter))
print(batch.Text)
print(batch.Label)


# モデル定義

# bidirectional lstm
class EncoderRNN(nn.Module):
    def __init__(self, emb_dim, h_dim, v_size, gpu=True, v_vec=None, batch_first=True):
        super(EncoderRNN, self).__init__()
        self.gpu = gpu
        self.h_dim = h_dim
        self.embed = nn.Embedding(v_size, emb_dim)
        if v_vec is not None:
            self.embed.weight.data.copy_(v_vec)
        self.lstm = nn.LSTM(emb_dim, h_dim, batch_first=batch_first,
                            bidirectional=True)

    def init_hidden(self, b_size):
        h0 = Variable(torch.zeros(1*2, b_size, self.h_dim))
        c0 = Variable(torch.zeros(1*2, b_size, self.h_dim))
        if self.gpu:
            h0 = h0.cuda()
            c0 = c0.cuda()
        return (h0, c0)

    def forward(self, sentence, lengths=None):
        self.hidden = self.init_hidden(sentence.size(0))
        emb = self.embed(sentence)
        packed_emb = emb

        if lengths is not None:
            lengths = lengths.view(-1).tolist()
            packed_emb = nn.utils.rnn.pack_padded_sequence(emb, lengths)

        out, hidden = self.lstm(packed_emb, self.hidden)

        if lengths is not None:
            out = nn.utils.rnn.pad_packed_sequence(output)[0]

        out = out[:, :, :self.h_dim] + out[:, :, self.h_dim:]

        return out

# attentionクラス。LSTMの隠れ層を入力として、各単語へのattentionを出力
class Attn(nn.Module):
    def __init__(self, h_dim):
        super(Attn, self).__init__()
        self.h_dim = h_dim
        self.main = nn.Sequential(
            nn.Linear(h_dim, 32),
            nn.ReLU(True),
            nn.Linear(32,1)
        )

    def forward(self, encoder_outputs):
        b_size = encoder_outputs.size(0)
        attn_ene = self.main(encoder_outputs.view(-1, self.h_dim)) # (b, s, h) -> (b * s, 1)
        return F.softmax(attn_ene.view(b_size, -1), dim=1).unsqueeze(2) # (b*s, 1) -> (b, s, 1)

# attentionを利用した文書分類
class AttnClassifier(nn.Module):
    def __init__(self, h_dim, c_num):
        super(AttnClassifier, self).__init__()
        self.attn = Attn(h_dim)
        self.main = nn.Linear(h_dim, c_num)


    def forward(self, encoder_outputs):
        attns = self.attn(encoder_outputs) #(b, s, 1)
        feats = (encoder_outputs * attns).sum(dim=1) # (b, s, h) -> (b, h)
        return F.log_softmax(self.main(feats),dim=1), attns  #dim=1

# 学習
def train_model(epoch, train_iter, optimizer, log_interval=1, batch_size=2):
    encoder.train()
    classifier.train()
    correct = 0
    for idx, batch in enumerate(train_iter):
        (x, x_l), y = batch.Text, batch.Label
        optimizer.zero_grad()
        encoder_outputs = encoder(x)
        output, attn = classifier(encoder_outputs)
        loss = F.nll_loss(output, y)
        loss.backward()
        optimizer.step()
        pred = output.data.max(1, keepdim=True)[1]
        correct += pred.eq(y.data.view_as(pred)).cpu().sum()
        if idx % log_interval == 0:
            print('train epoch: {} [{}/{}], acc:{}, loss:{}'.format(
            epoch, (idx+1)*len(x), len(train_iter)*batch_size,
            correct/float(log_interval * len(x)),
            loss.data[0]))
            correct = 0

# 検証
def test_model(epoch, test_iter):
    encoder.eval()
    classifier.eval()
    correct = 0
    for idx, batch in enumerate(test_iter):
        (x, x_l), y = batch.Text, batch.Label
        encoder_outputs = encoder(x)
        output, attn = classifier(encoder_outputs)
        pred = output.data.max(1, keepdim=True)[1]
        correct += pred.eq(y.data.view_as(pred)).cpu().sum()
    print('test epoch:{}, acc:{}'.format(epoch, correct/float(len(test))))

# ハイパーパラメータ
emb_dim = 300 #単語埋め込み次元
h_dim = 32 #lstmの隠れ層の次元
class_num = 2 #予測クラス数
lr = 0.001 #学習係数
epochs = 3 #エポック数


# モデル作成
encoder = EncoderRNN(emb_dim, h_dim, len(TEXT.vocab),gpu=True, v_vec = TEXT.vocab.vectors)
classifier = AttnClassifier(h_dim, class_num)

encoder.cuda()
classifier.cuda()

# モデル初期化
def weights_init(m):
    classname = m.__class__.__name__
    if hasattr(m, 'weight') and (classname.find('Embedding') == -1):
        nn.init.xavier_uniform(m.weight.data, gain=nn.init.calculate_gain('relu'))

for m in encoder.modules():
    print(m.__class__.__name__)
    weights_init(m)

for m in classifier.modules():
    print(m.__class__.__name__)
    weights_init(m)

# 最適化関数
optimizer = optim.Adam(chain(encoder.parameters(),classifier.parameters()), lr=lr)

# 訓練
for epoch in range(epochs):
    train_model(epoch + 1, train_iter, optimizer)
    test_model(epoch + 1, test_iter)

# モデル保存
dill.dump(encoder, open("../model/encoder.pkl","wb"))
dill.dump(classifier, open("../model/classifier.pkl","wb"))


# Attention可視化
# %html
# from IPython.display import HTML

# 単語とattentionの強さを受け取ってspanタグをつける関数
def highlight(word, attn):
    html_color = u'#%02X%02X%02X' % (255, int(255*(1 - attn)), int(255*(1 - attn)))
    return u'<span style="background-color: {}">{}</span>'.format(html_color, word)

# 単語番号と対応するattentionの配列を受け取って、spanタグで色付けされた文字列を返す関数
def mk_html(sentence, attns):
    def itos(word):
        word = TEXT.vocab.itos[word]
        return word.strip("<").strip(">")
    html = u""
    for word, attn in zip(sentence, attns):
        html += u' ' + highlight(
        itos(word),
        attn
        )
    return html + u"<br><br>"


# f = open("../results/attn.html", "w")
# res = ""
# for batch in test_iter:
#     x = batch.Text[0]
#     y = batch.Label
#     encoder_outputs = encoder(x)
#     output, attn = classifier(encoder_outputs)
#     pred = output.data.max(1, keepdim=True)[1]
#     a = attn.data[0,:,0]
#     res += u"正解{}:予測{}".format(str(y[0].data[0]), str(pred[0][0])) + mk_html(x.data[0], a)
# f.close()

f = open("../results/attn.html", "w")
for batch in test_iter:
    x = batch.text[0]
    y = batch.label
    encoder_outputs = encoder(x)
    output, attn = classifier(encoder_outputs)
    pred = output.data.max(1, keepdim=True)[1]
    a = attn.data[0,:,0]
    f.write( '\t'.join( (str(y[0].data), str(pred[0]), mk_html(x.data[0], a))) )
f.close()
