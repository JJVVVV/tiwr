import fcntl
import json
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import accuracy_score, f1_score
from toolkit.metric import MetricDict
from toolkit.training import Evaluator
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

from load_data_fns import DATASET_CLASSNUM_MAP, DatasetName, TextType


class Evaluator1(Evaluator):
    confused_use_ot = False
    save_results = True
    just_return_metric = True

    def calculate_metric_callback(self, all_labels: list, all_logits: list, mean_loss: float):
        all_labels = np.array(all_labels)
        all_logits = np.array(all_logits)
        text_type = TextType[self.config.text_type]
        dataset_name = DatasetName[self.config.dataset_name]
        match DATASET_CLASSNUM_MAP[dataset_name]:
            case 2:
                if "DATA_AUG_REP" in text_type.name and "DATA_AUG_REP4_FUSED" != text_type.name:
                    # all_logits: (num, 4)
                    all_ori_preds = (all_logits > 0).astype(int)
                    threshold = all_ori_preds.shape[1] >> 1
                    vote_pos = all_ori_preds.sum(axis=1)
                    all_preds = np.zeros_like(vote_pos)
                    pos_mask = vote_pos > threshold
                    neg_mask = ~pos_mask
                    controversial_mask = np.zeros_like(pos_mask).astype(bool) if all_ori_preds.shape[1] & 1 else vote_pos == threshold
                    all_preds[pos_mask] = 1
                    all_preds[neg_mask] = 0
                    # if controversial, then use original text
                    all_preds[controversial_mask] = all_ori_preds[controversial_mask][:, 0]
                    definite_mask = (vote_pos == all_ori_preds.shape[1]) | (vote_pos == 0)
                    confused_mask = ~(definite_mask | controversial_mask)
                    # # if confused, then use original text
                    if self.confused_use_ot:
                        all_preds[confused_mask] = all_ori_preds[confused_mask][:, 0]
                elif text_type == TextType.GAUSSIAN_LABEL:
                    # all_logtis: (num, 100)
                    all_preds = (np.argmax(all_logits, axis=1, keepdims=True) >= 50).astype(int)
                else:
                    # all_logtis: (num, 1)
                    all_preds = (all_logits > 0).astype(int)
            case _:
                all_preds = np.argmax(all_logits, axis=1, keepdims=True)

        if DATASET_CLASSNUM_MAP[dataset_name] == 2:
            acc = accuracy_score(all_labels, all_preds)
            f1 = f1_score(all_labels, all_preds, average="binary")
        else:
            acc = accuracy_score(all_labels, all_preds)
            f1 = f1_score(all_labels, all_preds, average="micro")

        if "DATA_AUG_REP" in text_type.name and "DATA_AUG_REP4_FUSED" != text_type.name:
            wrong_cnt_contro_confused = 0
            contro_cnt = controversial_mask.sum()
            confused_cnt = confused_mask.sum()
            if hasattr(self, "logger"):
                self.logger.info("Consistent:")
            else:
                print("Consistent:")
            mask = definite_mask
            if hasattr(self, "logger"):
                self.logger.info(f"{mask.sum()}/{mask.size}  {mask.sum()/mask.size*100:.2f}%")
            else:
                print(f"{mask.sum()}/{mask.size}  {mask.sum()/mask.size*100:.2f}%")
            part_acc = accuracy_score(y_true=all_labels[mask], y_pred=all_preds[mask])
            part_f1 = f1_score(y_true=all_labels[mask], y_pred=all_preds[mask])
            if hasattr(self, "logger"):
                self.logger.info(f"acc: {part_acc * 100:.2f}%\tf1: {part_f1 * 100:.2f}%\n")
                self.logger.info("Inconsistent: ")
            else:
                print(f"acc: {part_acc * 100:.2f}%\tf1: {part_f1 * 100:.2f}%\n")
                print("Inconsistent: ")
            mask = controversial_mask | confused_mask
            if hasattr(self, "logger"):
                self.logger.info(f"{mask.sum()}/{mask.size}  {mask.sum()/mask.size*100:.2f}%")
            else:
                print(f"{mask.sum()}/{mask.size}  {mask.sum()/mask.size*100:.2f}%")
            part_acc = accuracy_score(y_true=all_labels[mask], y_pred=all_preds[mask])
            part_f1 = f1_score(y_true=all_labels[mask], y_pred=all_preds[mask])
            if hasattr(self, "logger"):
                self.logger.info(f"acc: {part_acc * 100:.2f}%\tf1: {part_f1 * 100:.2f}%\n")
            else:
                print(f"acc: {part_acc * 100:.2f}%\tf1: {part_f1 * 100:.2f}%\n")

            for des, mask in zip(("controversial", "confused", "definite"), [controversial_mask, confused_mask, definite_mask]):
                if mask.sum() == 0:
                    continue
                if hasattr(self, "logger"):
                    self.logger.info(des + ":")
                    self.logger.info(f"{mask.sum()}/{mask.size}  {mask.sum()/mask.size*100:.2f}%")
                else:
                    print(des + ":")
                    print(f"{mask.sum()}/{mask.size}  {mask.sum()/mask.size*100:.2f}%")
                part_acc = accuracy_score(y_true=all_labels[mask], y_pred=all_preds[mask])
                part_f1 = f1_score(y_true=all_labels[mask], y_pred=all_preds[mask])
                if hasattr(self, "logger"):
                    self.logger.info(f"acc: {part_acc * 100:.2f}%\tf1: {part_f1 * 100:.2f}%\n")
                else:
                    print(f"acc: {part_acc * 100:.2f}%\tf1: {part_f1 * 100:.2f}%\n")
                wrong_cnt_contro_confused += mask.sum() * (1 - part_acc) if des != "definite" else 0
            if wrong_cnt_contro_confused:
                print(wrong_cnt_contro_confused)
                print(
                    round(
                        (len(self.dataset) * acc + (contro_cnt + confused_cnt) * 0.88 - (contro_cnt + confused_cnt - wrong_cnt_contro_confused))
                        / len(self.dataset)
                        * 100,
                        2,
                    )
                )

            bad_cases = dict()
            controversial_cases = dict()
            definite_cases = dict()
            confused_cases = dict()

            for i in range(len(self.dataset)):
                if all_preds[i] != all_labels[i]:
                    bad_cases[i] = {"text": self.dataset.texts_input[i].tolist(), "pred": all_preds[i].item(), "labels": all_labels[i].item()}
                if "DATA_AUG_REP" in text_type.name:
                    if controversial_mask.size and controversial_mask[i]:
                        controversial_cases[i] = {
                            "text": self.dataset.texts_input[i].tolist(),
                            "pred": all_preds[i].item(),
                            "labels": all_labels[i].item(),
                            "ori_labels": all_ori_preds[i].tolist(),
                        }
                    if confused_mask.size and confused_mask[i]:
                        confused_cases[i] = {
                            "text": self.dataset.texts_input[i].tolist(),
                            "pred": all_preds[i].item(),
                            "labels": all_labels[i].item(),
                            "ori_labels": all_ori_preds[i].tolist(),
                        }
                    if definite_mask.size and definite_mask[i]:
                        definite_cases[i] = {
                            "text": self.dataset.texts_input[i].tolist(),
                            "pred": all_preds[i].item(),
                            "labels": all_labels[i].item(),
                            "ori_labels": all_ori_preds[i].tolist(),
                        }
            good_cases_idxs = set(range(len(self.dataset))) - set(bad_cases.keys())
            metric_dict = MetricDict({"accuracy": acc * 100, "F1-score": f1 * 100, "loss": mean_loss})
            if hasattr(self, "logger"):
                self.logger.info(f"<{self.split.name:^14}>  {metric_dict}")

            file_path: Path = self.config.save_dir / "evaluator" / f"step={self.config.training_runtime['cur_step']}" / (self.split.name + ".json")
            if self.save_results:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "w") as f:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        data = json.dumps(
                            [dict(metric_dict), bad_cases, list(good_cases_idxs), controversial_cases, confused_cases, definite_cases],
                            ensure_ascii=False,
                        )
                        f.write(data)
                        f.flush()
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except IOError:
                        print("⚠️ Skip this operation because other programs are writing files ...")
            if self.just_return_metric:
                return metric_dict
            else:
                return (metric_dict, bad_cases, good_cases_idxs, controversial_cases, confused_cases, definite_cases, all_logits, all_labels)
        else:
            metric_dict = MetricDict({"accuracy": acc * 100, "F1-score": f1 * 100, "loss": mean_loss})
            if self.just_return_metric:
                return metric_dict
            else:
                return metric_dict, all_logits, all_labels
