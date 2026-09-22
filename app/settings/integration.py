# Add your integration-specific settings here
from .base import env

# Savannah Tracking advertises no rate limits, but their API's capacity is
# known to be small. Cap the per-collar fetches in flight against it across
# every runner instance (legacy cronjob: an asyncio.Semaphore(5)).
SAVANNAH_MAX_CONCURRENT_REQUESTS = env.int("SAVANNAH_MAX_CONCURRENT_REQUESTS", 5)
# How long a per-collar run waits for a slot before yielding to the next
# 5-minute schedule. Well under MAX_ACTION_EXECUTION_TIME so a run that does
# get a slot still has time to use it.
SAVANNAH_CONCURRENCY_MAX_WAIT_SECONDS = env.float("SAVANNAH_CONCURRENCY_MAX_WAIT_SECONDS", 180.0)
