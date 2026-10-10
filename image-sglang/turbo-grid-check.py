#!/usr/bin/env python3
"""Can this runtime serve Qwen-Image 2.1 Turbo as it was trained? Run with the image lane's
own python: exit 0 if it can, 1 with the reason on stderr if it cannot.

The Turbo samples on the eight-step sigma grid its model_index.json carries, which a runtime
follows only with sgl-project/sglang#43391 (the three qwen-image21-turbo-sigma-*.patch files
beside this one): the pipeline takes the grid from model_index.json, the pipeline config
keeps it, and the input stage samples on it. Without any one of the three the Turbo loads,
answers 200 and samples on a uniform schedule it was not trained for. A patch that does not
apply to a new pin is a note in the installer, not a failure, so the three are checked here
one by one: the config by what it does with a grid, the pipeline and the stage by the code
that carries it. A release that carries #43391 as merged passes as well.
"""
import dataclasses
import inspect
import sys


def missing() -> str:
    try:
        from sglang.multimodal_gen.configs.pipeline_configs.qwen_image21 import (
            QwenImage21PipelineConfig as Config,
        )
        from sglang.multimodal_gen.runtime.pipelines.qwen_image21 import (
            QwenImage21Pipeline as Pipeline,
        )
        from sglang.multimodal_gen.runtime.pipelines_core.stages.model_specific_stages.qwen_image21 import (
            QwenImage21InputValidationStage as Stage,
        )
    except Exception as e:  # noqa: BLE001 (any import failure means the same thing here)
        return f"the runtime has no Qwen-Image 2.1 pipeline to check ({type(e).__name__}: {e})"
    if "sample_sigmas" not in {f.name for f in dataclasses.fields(Config)}:
        return "its pipeline config has no place for a sigma grid"
    grid = [1.0, 0.75, 0.5, 0.25]
    if list(Config(sample_sigmas=grid).prepare_sigmas(None, 50)) != grid:
        return "its pipeline config does not sample on the grid it holds"
    if "sample_sigmas" not in inspect.getsource(Pipeline):
        return "its pipeline does not read the grid from model_index.json"
    if "prepare_sigmas" not in inspect.getsource(Stage):
        return "its input stage does not take the steps from the grid"
    return ""


if __name__ == "__main__":
    why = missing()
    if why:
        print(why, file=sys.stderr)
    sys.exit(1 if why else 0)
