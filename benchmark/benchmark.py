import argparse
import json
from itertools import chain
from random import sample, shuffle
from time import perf_counter
from typing import List, Optional, Tuple

import pandas as pd
import seaborn as sns
from openvino import AsyncInferQueue, CompiledModel, InferRequest, compile_model
from openvino.tools.benchmark.utils.utils import print_perf_counters_sort
from openvino.runtime import properties
from openvino_tokenizers import convert_tokenizer
from tqdm.auto import tqdm
from transformers import AutoTokenizer, PreTrainedTokenizerBase


def sample_texts(
    dataset_path: str,
    num_texts: int = 1000,
) -> List[Tuple[str, str]]:
    with open(dataset_path) as f:
        dataset = json.load(f)
    # Filter out the conversations with less than 2 turns.
    dataset = [data for data in dataset if len(data["conversations"]) >= 2]
    # Only keep the first two turns of each conversation.
    dataset = [
        (data["conversations"][0]["value"], data["conversations"][1]["value"]) for data in sample(dataset, k=num_texts)
    ]
    shuffle(dataset)
    return dataset


def benchmark_tokenizer_async(ov_tokenizer: CompiledModel, dataset: List[Tuple[str, str]]) -> Tuple[pd.Series, float]:
    def callback(
        ir: InferRequest,
        user_data: Tuple[List[int], float, int],
    ) -> None:
        end = perf_counter()
        times, start, idx = user_data
        times[idx] = end - start

    data_size = len(dataset) * 2
    async_queue = AsyncInferQueue(ov_tokenizer)
    async_queue.set_callback(callback)
    times = [0 for _ in range(data_size)]

    bench_start = perf_counter()
    for idx, prompt in tqdm(enumerate(chain.from_iterable(dataset)), total=data_size, desc="Async benchmark"):
        start = perf_counter()
        async_queue.start_async([prompt], (times, start, idx))
    async_queue.wait_all()
    elapsed = perf_counter() - bench_start

    results = pd.Series(data=times, name="OV_Async")

    return results, data_size / elapsed


def benchmark_tokenizers(
    ov_tokenizer: CompiledModel,
    hf_tokenizer: PreTrainedTokenizerBase,
    dataset: List[Tuple[str, str]],
    per_layer_stats: bool = False
) -> pd.DataFrame:
    columns = ["prompt", "OV", "HF"]
    results = []

    # warmup
    for repeat in range(1, 11):
        ov_tokenizer(["test " * repeat])
        hf_tokenizer(["test " * repeat])

    for prompt in tqdm(chain.from_iterable(dataset), total=len(dataset) * 2, desc="Sync benchmark"):
        res = [prompt]

        ov_start = perf_counter()
        ov_tokenizer([prompt])
        res.append(perf_counter() - ov_start)

        hf_start = perf_counter()
        hf_tokenizer([prompt])
        res.append(perf_counter() - hf_start)

        results.append(res)

    if per_layer_stats:
        print_perf_counters_sort([ov_tokenizer._infer_request.profiling_info])

    return pd.DataFrame(results, columns=columns)


def dump_latency_stats(results: pd.DataFrame, model_name: str) -> None:
    sorted_res = results.sort_values("Prompt Length, chars")
    sorted_res["OV vs HF"] = sorted_res["OV"] / sorted_res["HF"]
    sorted_res["OV_ASYNC vs HF"] = sorted_res["OV_ASYNC"] / sorted_res["HF"]

    sorted_res.to_csv(f"latency_res_{model_name}.csv", index=False)


def print_stats(results: pd.DataFrame, async_fps: Optional[float] = None) -> None:
    data_size = len(results)
    ov_fps = data_size / results["OV"].sum()
    hf_fps = data_size / results["HF"].sum()

    print(f"Sync:  OV: {ov_fps:.3f} FPS, HF: {hf_fps:.3f} FPS, OV/HF: {ov_fps/hf_fps}")
    print(f"Async: OV: {async_fps:.3f} FPS, HF: {hf_fps:.3f} FPS, OV/HF: {async_fps/hf_fps}")
    print("Latency and prompt stats:")
    stats = results.describe().drop("count")
    print(stats)


def build_plot(results: pd.DataFrame, save_file: Optional[str] = None, **kwargs) -> None:
    cmap = sns.cubehelix_palette(rot=-0.2, as_cmap=True)
    plot = (
        sns.relplot(
            data=results,
            x="OV_ASYNC",
            y="HF",
            hue="Prompt Length, chars",
            palette=cmap,
        )
        .set_xlabels("OpenVINO Async, sec")
        .set_ylabels("Huggingface, sec")
    )
    if kwargs.get("log"):
        plot.set(xscale="log").set(yscale="log")

    if (title := kwargs.get("title")) is not None:
        plot.fig.suptitle(title)

    max_latency = max(results["OV"].max(), results["HF"].max())
    for ax in plot.axes[0]:
        ax.plot([0, max_latency], [0, max_latency], linestyle="dashed", linewidth=1, color="r")

    if save_file is not None:
        plot.savefig(save_file)
    return plot


def main(
    checkpoint: str,
    dataset: str,
    num_pairs: int = 1000,
    trust: bool = False,
    log: bool = False,
    dump_latency: bool = False,
    per_layer_stats: bool = False,
    tput: bool = False,
) -> None:
    hf_tokenizer = AutoTokenizer.from_pretrained(checkpoint, trust_remote_code=trust)

    hint = properties.hint.PerformanceMode.THROUGHPUT if tput else properties.hint.PerformanceMode.LATENCY
    config = {
        properties.hint.performance_mode(): hint}
    if per_layer_stats:
        config[properties.enable_profiling()] = True

    ov_tokenizer = compile_model(convert_tokenizer(hf_tokenizer), "CPU", config)

    dataset = sample_texts(dataset, num_pairs)
    result_df = benchmark_tokenizers(ov_tokenizer, hf_tokenizer, dataset, per_layer_stats)
    async_results, async_fps = benchmark_tokenizer_async(ov_tokenizer, dataset)
    result_df = result_df.assign(OV_ASYNC=async_results.values)
    result_df["Prompt Length, chars"] = result_df["prompt"].apply(len)

    print_stats(result_df, async_fps)
    model_name = checkpoint.rsplit("/", 1)[-1]

    if dump_latency:
        dump_latency_stats(result_df, model_name)

    build_plot(result_df, f"latency_benchmark_{model_name}.jpeg", log=log, title=f"OV vs HF Latency\n{checkpoint}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenVINO Tokenizers Benchmark")
    parser.add_argument(
        "model_id",
        type=str,
        help=(
            "The model id of a tokenizer hosted in a model repo on huggingface.co "
            "or a path to a saved Huggingface tokenizer directory"
        ),
    )
    parser.add_argument("-d", "--dataset", type=str, default=None, help="Path to the dataset.")
    parser.add_argument(
        "-n",
        "--num_pairs",
        type=int,
        default=1000,
        help="Number of prompt/completion pairs to sample from the dataset.",
    )
    parser.add_argument(
        "--trust-remote-code",
        "--trust_remote_code",
        required=False,
        action="store_true",
        help=(
            "Pass `trust_remote_code=True` to `AutoTokenizer.from_pretrained`. It will "
            "execute code present on the Hub on your local machine."
        ),
    )
    parser.add_argument(
        "--log-scale",
        "--log_scale",
        required=False,
        action="store_true",
        help="Use log scale for the plot.",
    )
    parser.add_argument(
        "--dump-latency-stats",
        "--dump_latency_stats",
        required=False,
        action="store_true",
        help="Save csv file with latency stats.",
    )
    parser.add_argument(
        "--print-per-layer-stats",
        "--print_per_layer_stats",
        required=False,
        action="store_true",
        help="Print execution info for each tokenizer layer.",
    )
    parser.add_argument(
        "--tput",
        required=False,
        action="store_true",
        help="Use THROUGHPUT performance hint.",
    )

    args = parser.parse_args()
    main(
        args.model_id,
        args.dataset,
        args.num_pairs,
        trust=args.trust_remote_code,
        log=args.log_scale,
        dump_latency=args.dump_latency_stats,
        per_layer_stats=args.print_per_layer_stats,
        tput=args.tput,
    )
