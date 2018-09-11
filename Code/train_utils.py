import os
import sys
import torch
import torch.autograd as autograd
import torch.nn.functional as F
import torch.nn as nn
import torch.utils.data as data
import torch.optim as optim
import data_utils as data_utils
import datetime
import sklearn.metrics as metrics
import time
import numpy as np
from termcolor import colored
from tqdm import tqdm
import math


def _to_tensor(x_list, cuda=True):
    '''
    Convert a list of numpy arrays into a list of pytorch tensors
    '''
    if type(x_list) is not list:
        x_list = [x_list]

    res_list = []
    for x in x_list:
        x = torch.from_numpy(x)
        if cuda:
            x = x.cuda()
        res_list.append(x)

    if len(res_list) == 1:
        return res_list[0]
    else:
        return tuple(res_list)

def _to_numpy(x_list):
    '''
    Convert a list of tensor into a list of numpy arrays
    '''
    if type(x_list) is not list:
        x_list = [x_list]

    res_list = []
    for x in x_list:
        res_list.append(x.data.cpu().numpy())

    if len(res_list) == 1:
        return res_list[0]
    else:
        return tuple(res_list)

def _to_number(x):
    '''
    Convert a scalar tensor into a python number
    '''
    if isinstance(x, torch.Tensor):
        return x.item()
    else:
        return x

def _compute_score(y_pred, y_true, num_classes=2):
    '''
    Compute the accuracy, f1, recall and precision
    '''
    if num_classes == 2:
        average = "binary"
    else:
        average = "macro"

    acc       = metrics.accuracy_score( y_pred=y_pred, y_true=y_true)
    f1        = metrics.f1_score(       y_pred=y_pred, y_true=y_true, average="macro")
    recall    = metrics.recall_score(   y_pred=y_pred, y_true=y_true, average="macro")
    precision = metrics.precision_score(y_pred=y_pred, y_true=y_true, average="macro")

    return acc, f1, recall, precision

def train(train_data, dev_data, model, args):
    # get time stamp for snapshot path
    timestamp = str(int(time.time() * 1e7))
    out_dir = os.path.abspath(os.path.join(os.path.curdir, "tmp-runs", timestamp))
    print("Saving the model to {}\n".format(out_dir))
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    #
    G = model['G']
    P_1 = model['P_1']
    P_2 = model['P_2']

    optimizer_g = torch.optim.Adam(filter(lambda p: p.requires_grad, G.parameters()) , lr=args.lr)
    optimizer_p1 = torch.optim.Adam(filter(lambda p: p.requires_grad, P_1.parameters()) , lr=args.lr)
    optimizer_p2 = torch.optim.Adam(filter(lambda p: p.requires_grad, P_2.parameters()) , lr=args.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer_g, 'min', patience=args.patience, factor=0.1, verbose=True)

    optimizers = {}
    optimizers['g'] = optimizer_g
    optimizers['p1'] = optimizer_p1
    optimizers['p2'] = optimizer_p2

    best = 100
    best_path = ""
    sub_cycle = 0

    ep = 1
    while True:
        start = time.time()

        batches = data_utils.data_loader(train_data, args.batch_size)

        if args.dispatcher:
            for batch in batches:
                train_batch(model, batch, optimizers, args)
        else:
            for batch in tqdm(batches,
                    total=math.ceil(len(train_data['label'])/args.batch_size), dynamic_ncols=True):
                train_batch(model, batch, optimizers, args)

        end = time.time()
        print("{}, Epoch {:3d}, Time Cost: {} seconds, temperature: {:.4f}".format(
            datetime.datetime.now().strftime('%H:%M:%S'), ep, end-start,
            args.temperature))

        if ep % 10 == 0:
            print("Train:", end=" ")
            evaluate(train_data, model, args)

        print("Dev  :", end=" ")
        # visualizing rationales during training, this will slow down training process
        writer = data_utils.generate_writer(os.path.join(out_dir, str(ep)))
        cur_loss, _, _, _, _ = evaluate(dev_data, model, args, writer)
        data_utils.close_writer(writer)
        # cur_loss, _, _, _, _ = evaluate(dev_data, model, args, None)

        scheduler.step(cur_loss) # auto adjust the lr when loss stop improving

        if cur_loss < best:
            best = cur_loss
            torch.save(G.state_dict(), os.path.join(out_dir, str(ep)))
            best_path = os.path.join(out_dir, str(ep))
            print("Saved current best weights to {}\n".format(best_path))
            sub_cycle = 0

        else:
            sub_cycle += 1

        if sub_cycle == args.patience*2:
            break

        ep += 1

    print("End of training. Restore the best weights.")
    model.load_state_dict(torch.load(best_path))

    print("Best development performance during training")
    loss, acc, recall, precision, f1 = evaluate(dev_data, model, args)

    print("Deleting model snapshot")
    os.system("rm -rf {}/*".format(out_dir)) # delete model snapshot for space

    if args.save:
        print("Save the best model to director saved-runs")
        best_dir = os.path.abspath(os.path.join(os.path.curdir, "saved-runs", args.dataset + '_' +
            str(args.num_classes) + '_' + timestamp))

        if not os.path.exists(best_dir):
            os.makedirs(best_dir)

        best_dir = os.path.join(best_dir, 'best')
        torch.save(model, best_dir)
        print("Best model is saved to {:s}".format(best_dir))

        with open(best_dir+'_args.txt', 'w') as f:
            for attr, value in sorted(args.__dict__.items()):
                f.write("{}={}\n".format(attr.upper(), value))

    return loss, acc, recall, precision, f1, best_dir

def _get_mask(text_len, cuda):
    idxes = torch.arange(0, int(torch.max(text_len)),
                         out=torch.IntTensor(torch.max(text_len).item())).unsqueeze(0)

    if cuda:
        idxes = idxes.cuda()

    text_mask = (idxes < text_len.unsqueeze(1)).float().detach() # batch, text_len

    return text_mask

def train_batch(model, batch, optimizers, args):
    G = model['G']
    P_1 = model['P_1']
    P_2 = model['P_2']

    optimizer_g = optimizers['g']
    optimizer_p1 = optimizers['p1']
    optimizer_p2 = optimizers['p2']

    # ----------------
    # Train generator
    # ----------------

    G.train()
    optimizer_g.zero_grad()

    # get the current batch
    text, text_len, label, _ = batch

    text, label, text_len = _to_tensor([text, label, text_len], args.cuda)
    text_mask = _get_mask(text_len, args.cuda)

    # ------------------------------------------------------------------------
    # Run the network
    if args.mode == 'test':
        raise ValueError('Cannot use test mode to train network')

    # Run the model
    g_out, pred_rationale = G(text, text_len, args.temperature)

    # Compute loss
    loss_g =  F.cross_entropy(g_out[:,-1], label)

    # Compute loss for rationale selection
    pred_rationale = pred_rationale * text_mask
    text_len = text_len.float()

    # Penalize total number of selection
    prob_selection = torch.div(torch.sum(pred_rationale, dim=1), text_len)
    loss_selection = F.binary_cross_entropy(prob_selection,
            torch.ones_like(prob_selection) * args.l_selection_target)

    # penalize discontinuities
    prob_variation = torch.div(
            torch.sum(torch.abs(pred_rationale[:,1:]-pred_rationale[:,:-1])*text_mask[:,1:], dim=1),
            text_len)
    loss_variation = F.binary_cross_entropy(prob_variation, torch.zeros_like(prob_variation))

    # total loss
    loss_g = loss_g + loss_selection * args.l_selection + loss_variation * args.l_variation

    loss_g.backward(retain_graph=True)
    optimizer_g.step()

    # ----------------
    # Train Predictor_1
    # ----------------

    P_1.train()
    optimizer_p1.zero_grad()

    # Run the model
    p1_out = P_1(g_out, text_len, args.temperature)

    # Compute loss
    loss_p1 =  F.cross_entropy(p1_out, label)

    loss_p1.backward(retain_graph=True)
    optimizer_p1.step()

    # ----------------
    # Train Predictor_2
    # ----------------

    P_2.train()
    optimizer_p2.zero_grad()

    # Run the model
    p2_out = P_2(torch.sub(torch.ones(g_out.size()).cuda().float(), g_out), text_len, args.temperature)

    # Compute loss
    loss_p2 =  F.cross_entropy(p2_out, label)

    loss_p2.backward(retain_graph=True)
    optimizer_p2.step()


def evaluate_batch(model, batch, args, writer=None):
    model['G'].eval()
    model['P_1'].eval()
    model['P_2'].eval()

    # get the current batch
    text, text_len, label, raw = batch
    text, text_len, label = _to_tensor([text, text_len, label], args.cuda)
    text_mask = _get_mask(text_len, args.cuda)

    # Run the model
    out, pred_rationale = model['G'](text, text_len, args.temperature, hard=False)
    out_p1 = model['P_1'](out, text_len, args.temperature, hard=False)
    out_p2 = model['P_2'](out, text_len, args.temperature, hard=False)

    loss_g = _to_numpy(F.cross_entropy(out[:,-1], label, reduction='none'))
    loss_p1 = _to_numpy(F.cross_entropy(out_p1, label, reduction='none'))
    loss_p2 = _to_numpy(F.cross_entropy(out_p2, label, reduction='none'))

    loss = loss_g + loss_p1 + loss_p2
    pred_lbl = np.argmax(_to_numpy(out[:,-1]), axis=1)


    # Compute loss for rationale selection
    pred_rationale = pred_rationale * text_mask
    text_len = text_len.float()

    # compute probabilities of selection
    prob_selection = torch.div(torch.sum(pred_rationale, dim=1), text_len)
    loss_selection = F.binary_cross_entropy(prob_selection,
            torch.ones_like(prob_selection) * args.l_selection_target, reduction='none')

    prob_selection, loss_selection  = _to_numpy([prob_selection, loss_selection])

    # compute probabilities of variation
    prob_variation = torch.div(
            torch.sum(torch.abs(pred_rationale[:,1:]-pred_rationale[:,:-1])*text_mask[:,1:], dim=1),
            text_len)
    loss_variation = F.binary_cross_entropy(prob_variation, torch.zeros_like(prob_variation),
            reduction='none')

    prob_variation, loss_variation  = _to_numpy([prob_variation, loss_variation])

    label, pred_rationale = _to_numpy([label, pred_rationale])
    """
    if writer:
        data_utils.write_human(writer['human'], raw, pred_lbl, label, pred_rationale, False)

        data_utils.write_machine(writer['machine'], args.dataset, raw, label, pred_rationale)

        #new_raw, new_pred, new_true, new_rat = data_utils.filter_rationale(raw, pred_lbl, label, pred_rationale)

        #data_utils.write_human(writer['filtered_human'], new_raw, new_pred, new_true, new_rat, False)
        #data_utils.write_machine(writer['filtered_machine'], args.dataset, new_raw, new_true, new_rat)
    """

    return {
            'true_lbl':       label,
            'pred_lbl':     pred_lbl,
            'loss':       loss,
            #'loss_selection': loss_selection,
            #'loss_variation': loss_variation,
            #'prob_selection': prob_selection,
            #'prob_variation': prob_variation,
            }

def evaluate(test_data, model, args, writer=None):
    total = {}

    batches = data_utils.data_loader(test_data, args.batch_size)
    for batch in batches:
        cur = evaluate_batch(model, batch, args, writer)

        # store results of current batch
        for key, value in cur.items():
            if key not in total:
                total[key] = value
            else:
                total[key] = np.concatenate((total[key], value))

    loss_lbl = np.mean(total['loss'])
    #loss_selection = np.mean(total['loss_selection'])
    #loss_variation = np.mean(total['loss_variation'])
    #prob_selection = np.mean(total['prob_selection'])
    #prob_variation = np.mean(total['prob_variation'])


    acc, f1, recall, precision = _compute_score(
            y_pred=total['pred_lbl'], y_true=total['true_lbl'], num_classes=args.num_classes)

    print("{} {:s} {:.6f}"
        "{:s} {:>7.4f}, {:s} {:>7.4f}, {:s} {:>7.4f}, {:s} {:>7.4f}".format(
            datetime.datetime.now().strftime('%H:%M:%S'),
            colored("loss", "red"),
            loss_lbl,
            colored(" acc", "blue"),
            acc,
            colored("recall", "blue"),
            recall,
            colored("precision", "blue"),
            precision,
            colored("f1", "blue"),
            f1))

    return loss_lbl, acc, recall, precision, f1
