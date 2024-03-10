# python vis_exp.py --from_pretrained data/trained/pretrain_LILY.bin  --pre_dataset ytb --prefix merge+

from pathlib import Path
from utils.cli import get_parser
from pretrain import set_cuda,get_local_rank
from utils.dataset.dataset_init import load_dataloader
from utils.misc import get_output_dir, set_seed, NoneLogger, logo_print, exp_saver, get_logger
from lily import Lily, BERT_CONFIG_FACTORY

from utils.dataset.all_dataset import YTbDataset
import torch
import random
import numpy as np
from utils.dataset.common import (
    load_json_data,
    perm2num,
    generate_negative_trajectories,
    load_shuffler,
    ytb_get_key,
    _check_enough_images,
    load_trajectories,
    ytb_generate_trajectory_from_listing,
    randomize_regions,
    randomize_tokens,
    load_tokens,
    generate_trajectory_out_listing,
    generate_trajectory_from_listing,
    merge_images,
    merge_frames,
    get_headings,
    shuffle_different,
    shuffle_non_adjacent,
    load_nav_graphs,
    load_distances,
    get_viewpoints,
    save_json_data,
    tokenize,
    InstructionGenerator,
    RephraseInstructionGenerator,
    ConcatenateInstructionGenerator,  
    YTBRephraseInstructionGenerator,
)

from tqdm import tqdm

from torch.utils.data import RandomSampler, SequentialSampler, DataLoader

from transformers import BertTokenizer
from utils.dataset.features_reader import FeaturesReader, BnBFeaturesReader, YTbFeaturesReader, PanoFeaturesReader

class VisDataset(YTbDataset):
    
    def __getitem__(self, index: int):

        
        # get a random listing_id
        
        listing_id = self._listing_ids[index]


        # select negative and positive photo ids
        (
            positive_ids,
            negative_captions,
            negative_images,
            negative_random,
            order_labels
        ) = self._pick_photo_ids(listing_id)

        print(positive_ids)


        # get the order label of trajectory
        ordering_target = []
        order_atteneded_visual_feature = 1
        
        prob_order = 1
            
        for key in order_labels:
            if key == "normal_idx" or key == "negative_captions_idx":
                # Skip normal_idx and negative_captions_idx and consider only negative_images_idx
                continue
            else:
                for random_order_path in range(len(order_labels[key])):
                    if prob_order < 0.7:
                        order_atteneded_visual_feature = 1 # 1 indicates random and 0 indicates normal
                        temp = [v for v in order_labels[key][random_order_path] ]
                        # If the path length is too short, it is automatically filled to the longest path
                        temp +=  [-1] * (self.args.max_path_length - len(positive_ids))
                        ordering_target.append(temp)
                    else:
                        order_atteneded_visual_feature = 0 # 1 indicates random and 0 indicates normal
                        ordering_target.append([i for i in range(len(positive_ids))] + \
                                                [-1] * (self.args.max_path_length - len(positive_ids)))

        # get the positive pair
        build_instruction = random.choice(self._build_instructions)
        self.templete = None
        
        instructions = [self.generate_instruction(build_instruction,positive_ids)]
        f, b, p, m = self._get_visual_features(positive_ids)
        features, boxes, probs, masks = [f], [b], [p], [m] # This feature will patch to the longest length (8)

        
        
        if self._traj_judge: # Trajectory judgment task
            negative_traj = negative_captions + negative_images + negative_random
            for traj in negative_traj:
                instructions += [instructions[0]]
                f, b, p, m = self._get_visual_features(traj)
                features += [f]
                boxes += [b]
                probs += [p]
                masks += [m]

        else:
            # get the negative captions
            for traj in negative_captions:
                instructions += [self.generate_instruction(build_instruction,traj)]
                features += [features[0]]
                boxes += [boxes[0]]
                probs += [probs[0]]
                masks += [masks[0]]

            if self.args.negative_style == 'shuffle_instruction':
                # get the negative captions
                for traj in negative_images:
                    instructions += [self.generate_instruction(build_instruction,traj)]
                    features += [features[0]]
                    boxes += [boxes[0]]
                    probs += [probs[0]]
                    masks += [masks[0]]
            else:
                # get the negative images
                for traj in negative_images:
                    instructions += [instructions[0]]
                    f, b, p, m = self._get_visual_features(traj)
                    features += [f]
                    boxes += [b]
                    probs += [p]
                    masks += [m]

            # get the random images
            for traj in negative_random:
                instructions += [instructions[0]]
                f, b, p, m = self._get_visual_features(traj)
                features += [f]
                boxes += [b]
                probs += [p]
                masks += [m]


        # convert data into tensors
        image_features = torch.from_numpy(np.array(features)).float()
        image_boxes = torch.from_numpy(np.array(boxes)).float()
        image_probs = torch.from_numpy(np.array(probs)).float()
        image_masks = torch.from_numpy(np.array(masks)).long()
        instr_tokens = torch.from_numpy(np.array(instructions)).long()
        instr_mask = instr_tokens > 0
        segment_ids = torch.zeros_like(instr_tokens)
        instr_highlights = torch.zeros((image_features.shape[0], 0)).long()


        # randomly mask image features
        if self._masked_vision:
            image_features, image_targets, image_targets_mask = randomize_regions(
                image_features, image_probs, image_masks
            )
        else:
            image_targets = torch.ones_like(image_probs) / image_probs.shape[-1]
            image_targets_mask = torch.zeros_like(image_masks)

        # randomly mask instruction tokens
        if self._masked_language:
            instr_tokens, instr_targets = randomize_tokens(
                instr_tokens, instr_mask, self._tokenizer, self.args
            )
        else:
            instr_targets = torch.ones_like(instr_tokens) * -1

        # construct null return items
        co_attention_mask = torch.zeros(
            2, self.args.max_path_length * self.args.max_num_boxes, self.args.max_instruction_length
        ).long()
        
        ordering_target = torch.tensor(ordering_target)
        if self._training:
            ranking_target = torch.tensor(0)
        else:
            ranking_target = torch.zeros(image_features.shape[0]).bool()
            ranking_target[0] = 1
        
        return (
            ranking_target,
            image_features,
            image_boxes,
            image_masks,
            image_targets,
            image_targets_mask,
            instr_tokens,
            instr_mask,
            instr_targets,
            instr_highlights,
            segment_ids,
            co_attention_mask,
            torch.tensor(self.get_listing_ids(listing_id)).long(),
            torch.ones(image_features.shape[0]).bool(),
            ordering_target,
            order_atteneded_visual_feature,
        )





# command line parsing
parser = get_parser()
parser.add_argument("--final", default=False, action="store_true")
args = parser.parse_args()

# get device settings
default_gpu, _, device = set_cuda(args)
logger = NoneLogger()


# create data loaders
local_rank = get_local_rank(args)
train_data_loader, test_data_loader, val_seen_data_loader, val_unseen_data_loader = load_dataloader(args, default_gpu, logger, local_rank)

# load pre-trained model

# Loading model
logger.info(f"Loading model")
config = BERT_CONFIG_FACTORY[args.model_name].from_json_file(args.config_file)


config.args = args

if len(args.from_pretrained) == 0:  # hack for catching --from_pretrained ""
    model = Lily(config)
else:
    model = Lily.from_pretrained(
        args.from_pretrained, config, default_gpu=default_gpu
    )

model.to(device)


def load_features_reader(args) -> FeaturesReader:
    if args.pre_dataset == 'ytb':
        return YTbFeaturesReader(args.ytb_feature)
    
def get_testset_path(args) -> str:
    testset_path = {}
    if args.ranking or args.not_traj_judge_data:
        if args.negative_style == "normal":
            negative_style = ""
        else:
            negative_style = args.negative_style + "_"
        testset_path["ranking"] = get_path(args, negative_style)
    if args.traj_judge and not args.ranking:
        # when ranking and traj_judge work simultaneously, use ranking's testset
        testset_path["traj"] =  get_path(args, "traj_")
    
    return testset_path

def get_path(args, task_prefix) ->str:
    return f"data/YouTube-VLN/{args.pre_dataset}/{args.prefix}{task_prefix}testset{args.feather_note}.json"



# construct model inputs
caption_path = f"data/YouTube-VLN/{args.pre_dataset}/{args.prefix}{args.pre_dataset}_train{args.feather_note}.json"
tokenizer = BertTokenizer.from_pretrained(args.bert_tokenizer)
features_reader = load_features_reader(args)
separators = ("then", "and", ",", ".") if args.separators else ("[SEP]",)
testset_path = get_testset_path(args)


Datset = VisDataset(
    args = args,
    caption_path=caption_path,
    tokenizer=tokenizer,
    features_reader=features_reader,
    masked_vision=False,
    masked_language=False,
    training=True,
    separators=separators,
    testset_path=testset_path,
)

train_sampler = RandomSampler(Datset)

train_data_loader = DataLoader(
        Datset,
        sampler=train_sampler,
        batch_size=8,
        num_workers=args.num_workers,
        pin_memory=True,
    )


for step, batch in enumerate(tqdm(train_data_loader, disable= not (default_gpu))):

    batch = tuple(
            t.cuda(device=device, non_blocking=True) if hasattr(t, "cuda") else t
            for t in batch
        )
