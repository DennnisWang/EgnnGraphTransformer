import argparse
import logging
import numpy as np
import os
import sys
import time
import torch
import torch.multiprocessing as mp
from typing import Dict, List, Tuple

from unimol_tools import UniMolRepr

from utils import parsing
from utils.data_utils import get_graph_features_from_smi, load_vocab, make_vocab, \
    tokenize_selfies_from_smiles, tokenize_smiles, get_graph_features_from_smis
from utils.train_utils import log_tensor, set_seed, setup_logger
from dgl.data.utils import save_graphs

G_vocab = {}  # global vocab


def get_preprocess_parser():
    parser = argparse.ArgumentParser("preprocess")
    parsing.add_common_args(parser)
    parsing.add_preprocess_args(parser)

    return parser


def tokenize(fns: Dict[str, List[Tuple[str, str]]], output_path: str, repr_start: str, repr_end: str):
    assert repr_start == "smiles", f"{repr_start} input provided. Only smiles inputs are supported!"

    if repr_end == "smiles":
        tokenize_line = tokenize_smiles
    elif repr_end == "selfies":
        tokenize_line = tokenize_selfies_from_smiles
    else:
        raise ValueError(f"{repr_end} output required. Only smiles and selfies outputs are supported!")

    ofns = {}

    for phase, file_list in fns.items():
        ofns[phase] = []

        for src_file, tgt_file in file_list:
            src_output = os.path.join(output_path, f"{repr_end}_tokenized_{os.path.basename(src_file)}")
            tgt_output = os.path.join(output_path, f"{repr_end}_tokenized_{os.path.basename(tgt_file)}")

            for fn, ofn in [(src_file, src_output),
                            (tgt_file, tgt_output)]:
                if os.path.exists(ofn):
                    logging.info(f"Found {ofn}, skipping tokenization.")
                    continue

                with open(fn, "r") as f, open(ofn, "w") as of:
                    logging.info(f"Tokenizing input {fn} into {ofn}")
                    for i, line in enumerate(f):
                        line = "".join(line.strip().split())
                        newline = tokenize_line(line)
                        of.write(f"{newline}\n")
                    logging.info(f"Done, total lines: {i + 1}")

            ofns[phase].append((src_output, tgt_output))

    return ofns


def get_token_ids(tokens: list, vocab: Dict[str, int], max_len: int) -> Tuple[List, int]:
    # token_ids = [vocab["_SOS"]]               # shouldn't really need this
    token_ids = []
    token_ids.extend([vocab[token] for token in tokens])
    token_ids = token_ids[:max_len - 1]
    token_ids.append(vocab["_EOS"])

    lens = len(token_ids)
    while len(token_ids) < max_len:
        token_ids.append(vocab["_PAD"])

    return token_ids, lens


def get_seq_features_from_line(_args) -> Tuple[np.ndarray, int, np.ndarray, int]:
    i, src_line, tgt_line, max_src_len, max_tgt_len = _args
    assert isinstance(src_line, str) and isinstance(tgt_line, str)
    if i > 0 and i % 10000 == 0:
        logging.info(f"Processing {i}th SMILES")

    global G_vocab

    src_tokens = src_line.strip().split()
    if not src_tokens:
        src_tokens = ["C", "C"]  # hardcode to ignore
    tgt_tokens = tgt_line.strip().split()
    src_token_ids, src_lens = get_token_ids(src_tokens, G_vocab, max_len=max_src_len)
    tgt_token_ids, tgt_lens = get_token_ids(tgt_tokens, G_vocab, max_len=max_tgt_len)

    src_token_ids = np.array(src_token_ids, dtype=np.int32)
    tgt_token_ids = np.array(tgt_token_ids, dtype=np.int32)

    return src_token_ids, src_lens, tgt_token_ids, tgt_lens


def binarize_s2s(src_file: str, tgt_file: str, prefix: str, output_path: str,
                 max_src_len: int, max_tgt_len: int, num_workers: int = 1):
    output_file = os.path.join(output_path, f"{prefix}.npz")
    logging.info(f"Binarizing (s2s) src {src_file} and tgt {tgt_file}, saving to {output_file}")

    with open(src_file, "r") as f:
        src_lines = f.readlines()

    with open(tgt_file, "r") as f:
        tgt_lines = f.readlines()

    logging.info("Getting seq features")
    start = time.time()

    p = mp.Pool(num_workers)
    seq_features_and_lengths = p.imap(
        get_seq_features_from_line,
        ((i, src_line, tgt_line, max_src_len, max_tgt_len)
         for i, (src_line, tgt_line) in enumerate(zip(src_lines, tgt_lines)))
    )

    p.close()
    p.join()

    seq_features_and_lengths = list(seq_features_and_lengths)

    logging.info(f"Done seq featurization, time: {time.time() - start}. Collating")
    src_token_ids, src_lens, tgt_token_ids, tgt_lens = zip(*seq_features_and_lengths)

    src_token_ids = np.stack(src_token_ids, axis=0)
    src_lens = np.array(src_lens, dtype=np.int32)
    tgt_token_ids = np.stack(tgt_token_ids, axis=0)
    tgt_lens = np.array(tgt_lens, dtype=np.int32)

    np.savez(
        output_file,
        src_token_ids=src_token_ids,
        src_lens=src_lens,
        tgt_token_ids=tgt_token_ids,
        tgt_lens=tgt_lens
    )


def merge_chunks(chunks):
    """
    将多个子列表合并成一个大列表。

    参数：
    chunks (list of list): 包含多个子列表的列表。

    返回值：
    list: 合并后的大列表。
    """
    merged_list = []

    for chunk in chunks:
        merged_list.extend(chunk)

    return merged_list


def chunk_list(lst, num_chunks):
    """
    将列表按照指定数量的块进行分割。

    参数：
    lst (list): 要分割的列表。
    num_chunks (int): 要分割成的块数。

    返回值：
    list of list: 包含分割后的子列表的列表。
    """
    # 计算每个块的大小
    chunk_size = len(lst) // num_chunks
    remainder = len(lst) % num_chunks  # 处理余数，确保所有元素都被分配

    chunks = []
    start = 0

    for i in range(num_chunks):
        if i < remainder:
            # 前 remainder 个块每个块大小加 1
            end = start + chunk_size + 1
        else:
            # 后面的块每个块大小为 chunk_size
            end = start + chunk_size

        chunks.append(lst[start:end])
        start = end

    return chunks




def clean_none_coordinates(graphs, seq_features):
    new_seq_features = []
    new_graphs = []
    for i,seq_feature in enumerate(seq_features):
        if graphs[i] is None:
            continue
        else:
            new_graphs.append(graphs[i])
            new_seq_features.append(seq_feature)

    return new_graphs, new_seq_features

def initial_global(vocab):
    global G_vocab
    G_vocab = vocab
def binarize_g2s(src_file: str, tgt_file: str, prefix: str, output_path: str,
                 max_src_len: int, max_tgt_len: int, num_workers: int = 1):
    output_file = os.path.join(output_path, f"{prefix}.npz")
    graph_output_file = os.path.join(output_path, f"{prefix}.bin")

    logging.info(f"Binarizing (g2s) src {src_file} and tgt {tgt_file}, saving to {output_file}")
    logging.info(f"Binarizing (g2s) src {src_file} graph features, saving to {graph_output_file}")

    with open(src_file, "r") as f:
        # lines = f.readlines()[164104:164106]
        src_lines = f.readlines()

    with open(tgt_file, "r") as f:
        tgt_lines = f.readlines()

    logging.info("Getting seq features")
    start = time.time()

    p = mp.Pool(num_workers,initializer=initial_global,initargs=[G_vocab])
    seq_features_and_lengths = p.imap(
        get_seq_features_from_line,
        ((i, src_line, tgt_line, max_src_len, max_tgt_len)
         for i, (src_line, tgt_line) in enumerate(zip(src_lines, tgt_lines)))
    )

    p.close()
    p.join()

    seq_features_and_lengths = list(seq_features_and_lengths)

    logging.info(f"Done seq featurization, time: {time.time() - start}. Collating")

    logging.info("Getting graph features")
    start = time.time()
    
    
#     p = mp.Pool(num_workers)
    
#     graph_features_and_lengths = p.imap(
#         get_graph_features_from_smi,
#         ((i, "".join(line.split()), False) for i, line in enumerate(src_lines))
#     )
    
#     p.close()
#     p.join()
    graphs = get_graph_features_from_smis(src_lines)
#     chunks = chunk_list(src_lines, num_workers)
#     p = mp.Pool(num_workers)
#     graph_features_and_lengths = p.imap(
#         get_graph_features_from_smis,
#         ((lines) for i, lines in enumerate(chunks))
#     )
#     p.close()
#     p.join()
#
#     graph_features_and_lengths = merge_chunks(graph_features_and_lengths)
#     graphs = list(graph_features_and_lengths)

    logging.info(f"Done graph featurization, time: {time.time() - start}. ")
    
    # graphs = list(graph_features_and_lengths)
    graphs, seq_features_and_lengths = clean_none_coordinates(graphs, seq_features_and_lengths)
    logging.info(f"Done graph featurization, time: {time.time() - start}.Size: {len(graphs)} Collating and saving...")
    src_token_ids, src_lens, tgt_token_ids, tgt_lens = zip(*seq_features_and_lengths)

    src_token_ids = np.stack(src_token_ids, axis=0)
    src_lens = np.array(src_lens, dtype=np.int32)
    tgt_token_ids = np.stack(tgt_token_ids, axis=0)
    tgt_lens = np.array(tgt_lens, dtype=np.int32)

    logging.info(f"Starting to save sequences features.")
    np.savez(
        output_file,
        src_token_ids=src_token_ids,
        src_lens=src_lens,
        tgt_token_ids=tgt_token_ids,
        tgt_lens=tgt_lens
    )
    logging.info(f"Starting to save graphs features.")
    save_graphs(graph_output_file, graphs)








def preprocess_main(args):
    parsing.log_args(args)

    os.makedirs(args.preprocess_output_path, exist_ok=True)

    fns = {
        "train": [(args.train_src, args.train_tgt)],
        "val": [(args.val_src, args.val_tgt)],
        "test": [(args.test_src, args.test_tgt)]
    }

    if not args.representation_start == args.representation_end:
        assert args.do_tokenize, f"Different representations, start: {args.representation_start}, " \
                                 f"end: {args.representation_end}. Please set '--do_tokenize'"

    if args.do_tokenize:
        ofns = tokenize(fns=fns,
                        output_path=args.preprocess_output_path,
                        repr_start=args.representation_start,
                        repr_end=args.representation_end)
        fns = ofns  # just pass the handle of tokenized files

    vocab_file = os.path.join(args.preprocess_output_path,
                              f"vocab_{args.representation_end}.txt")
    if not os.path.exists(vocab_file):
        make_vocab(
            fns=fns,
            vocab_file=vocab_file,
            tokenized=True
        )

    if args.make_vocab_only:
        logging.info(f"--make_vocab_only flag detected. Skipping featurization")
        exit(0)

    global G_vocab
    G_vocab = load_vocab(vocab_file)

    if args.model == "s2s":
        binarize = binarize_s2s
    elif args.model.startswith("g2s"):
        binarize = binarize_g2s
    else:
        raise ValueError(f"Model {args.model} not supported!")

    for phase, file_list in fns.items():
        for i, (src_file, tgt_file) in enumerate(file_list):
            binarize(
                src_file=src_file,
                tgt_file=tgt_file,
                prefix=f"{phase}_{i}",
                output_path=args.preprocess_output_path,
                max_src_len=args.max_src_len,
                max_tgt_len=args.max_tgt_len,
                num_workers=args.num_workers
            )


if __name__ == "__main__":
    preprocess_parser = get_preprocess_parser()
    args = preprocess_parser.parse_args()

    # set random seed
    set_seed(args.seed)

    # logger setup
    logger = setup_logger(args)

    np.set_printoptions(threshold=sys.maxsize)
    torch.set_printoptions(profile="full")

    preprocess_main(args)
