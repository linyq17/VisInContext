"""
SEED_Beach is closed-ended VQA evaluation.
For example:
Q. A1, A2, A3, A4. Then select one answer from A1, A2, A3, A4.

As in https://github.com/AILab-CVC/SEED-Bench/blob/main/eval.py,
we simply use losses as rank to determine the answer.
"""
import os
from src.eval.data.vqa_dataset import VQADataset
from src.eval.models import eval_base_model
from src.eval.eval_tasks.utils.vqa_metric import compute_vqa_accuracy, postprocess_vqa_generation
from src.eval.eval_tasks.utils.ok_vqa_utils import postprocess_ok_vqa_generation
import more_itertools
from src.eval.eval_tasks.util import *
from tqdm import tqdm
import json
import uuid

def evaluate_vqa(
    config: dict,
    eval_model: eval_base_model.BaseEvalModel,
    seed: int = 42,
    max_generation_length: int = 5,
    num_beams: int = 3,
    length_penalty: float = -2.0,
    num_shots: int = 8,
    eval_prompt_style: str = "flamingo",
    dataset_name: str = "vqav2",
):
    """
    ...
    Args:
        config (dict): Configuration dictionary.
        ...
        dataset_name (string): Type of VQA dataset, currently supports vqav2, ok_vqa. Defaults to vqav2.
    Returns:
        float: Accuracy score
    """
    if num_shots <= 8:
        batch_size = config['general']['batch_size']
    else:
        batch_size = 4
    num_samples = config['general']['num_samples']
    query_set_size = config['general']['query_set_size']

    # Get dataset configuration
    dataset_config = config['datasets'].get(dataset_name)
    if dataset_config is None:
        raise ValueError(f"Unsupported dataset: {dataset_name}")

    train_image_dir_path = os.path.join(config['general']['data_root'], dataset_config['train_image_dir_path'])
    train_questions_json_path = os.path.join(config['general']['data_root'], dataset_config['train_questions_json_path'])
    train_annotations_json_path = os.path.join(config['general']['data_root'], dataset_config['train_annotations_json_path'])
    test_image_dir_path = os.path.join(config['general']['data_root'], dataset_config['test_image_dir_path'])
    test_questions_json_path = os.path.join(config['general']['data_root'], dataset_config['test_questions_json_path'])
    test_annotations_json_path = os.path.join(config['general']['data_root'], dataset_config['test_annotations_json_path'])


    train_dataset = VQADataset(
        image_dir_path=train_image_dir_path,
        question_path=train_questions_json_path,
        annotations_path=train_annotations_json_path,
        is_train=True,
        dataset_name=dataset_name,
    )

    test_dataset = VQADataset(
        image_dir_path=test_image_dir_path,
        question_path=test_questions_json_path,
        annotations_path=test_annotations_json_path,
        is_train=False,
        dataset_name=dataset_name,
    )

    effective_num_shots = num_shots if num_shots > 0 else 2
    # effective_num_shots = num_shots # previously I always set effective_num_shots = num_shots

    test_dataset = prepare_eval_samples(
        test_dataset,
        num_samples if num_samples > 0 else len(test_dataset),
        seed,
    )

    in_context_samples = get_query_set(train_dataset, query_set_size, seed)
    predictions = []

    for batch in more_itertools.chunked(
        tqdm(test_dataset, desc=f"Running seed bench vqa inference {dataset_name.upper()} shots={num_shots}"),
        batch_size,
    ):
        batch_demo_samples = sample_batch_demos_from_query_set(
            in_context_samples, effective_num_shots, len(batch)
        )

        batch_images = []
        batch_text = []
        for i in range(len(batch)):
            if num_shots > 0:
                context_images = [x["image"] for x in batch_demo_samples[i]]
            else:
                context_images = []
            batch_images.append(context_images + [batch[i]["image"]])

            context_text = "".join(
                [
                    eval_model.vqa_prompt(
                        question=x["question"], answer=x["answers"][0]
                    )
                    for x in batch_demo_samples[i]
                ]
            )

            # Keep the text but remove the image tags for the zero-shot case
            if num_shots == 0:
                context_text = context_text.replace("<visual>", "")

            batch_text.append(
                context_text + eval_model.vqa_prompt(question=batch[i]["question"])
            )
        with torch.no_grad():
            outputs = eval_model.get_outputs(
                batch_images=batch_images,
                batch_text=batch_text,
                max_generation_length=max_generation_length,
                num_beams=num_beams,
                length_penalty=length_penalty,
            )

        process_function = (
            postprocess_vqa_generation
            if dataset_name == "vqav2"
            else postprocess_ok_vqa_generation
        )

        new_predictions = map(process_function, outputs)
        predictions.extend(
            [
                {"answer": p, "question_id": sample["question_id"]}
                for p, sample in zip(new_predictions, batch)
            ]
        )
        # print(batch_text[-1])
        # print(predictions[-1])
    # save the predictions to a temporary file
    random_uuid = str(uuid.uuid4())
    with open(f"{dataset_name}results_{random_uuid}.json", "w") as f:
        f.write(json.dumps(predictions, indent=4))

    acc = compute_vqa_accuracy(
        f"{dataset_name}results_{random_uuid}.json",
        test_questions_json_path,
        test_annotations_json_path,
    )

    # delete the temporary file
    os.remove(f"{dataset_name}results_{random_uuid}.json")
    return acc
