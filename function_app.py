import logging
import os

import azure.functions as func
from azure.identity import DefaultAzureCredential
from governance import Governance, Settings

app = func.FunctionApp()


@app.timer_trigger(schedule="%TIMER_SCHEDULE%", arg_name="timer", run_on_startup=False, use_monitor=True)
def enforce_foundry_policy(timer: func.TimerRequest) -> None:
    settings = Settings.from_env()
    governance = Governance(settings, DefaultAzureCredential())
    result = governance.run()
    logging.info("Governance run summary: %s", result)
    if result["errors"]:
        raise RuntimeError(f"Governance run had {result['errors']} error(s)")
