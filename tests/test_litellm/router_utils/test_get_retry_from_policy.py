import pytest

from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
from litellm.litellm_core_utils.exception_mapping_utils import exception_type
from litellm.router_utils.get_retry_from_policy import get_num_retries_from_retry_policy
from litellm.types.router import RetryPolicy


def test_get_num_retries_from_retry_policy_classifies_mapped_deadline_exceeded_as_timeout():
    """End-to-end pin: a DeadlineExceeded, once run through exception_type(), must still be
    classified by Router's retry-policy lookup as a Timeout -- proving the two are wired
    together. Router.get_allowed_fails_from_policy shares the byte-identical
    isinstance(exception, litellm.Timeout) primitive, so this single test pins both call
    sites' classification contract."""
    try:
        exception_type(
            model="gpt-4",
            original_exception=DeadlineExceeded("simulated"),
            custom_llm_provider="openai",
        )
        raise AssertionError("exception_type() should have raised")
    except AssertionError:
        raise
    except Exception as mapped:
        retries = get_num_retries_from_retry_policy(
            exception=mapped,
            retry_policy=RetryPolicy(TimeoutErrorRetries=7),
        )
        assert retries == 7
