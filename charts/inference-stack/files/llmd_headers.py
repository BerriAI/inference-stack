from litellm.integrations.custom_logger import CustomLogger


class LlmdHeaders(CustomLogger):
    """Tell llm-d which team sent a request, so it can share capacity fairly between teams."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        fairness_id = user_api_key_dict.team_id or user_api_key_dict.user_id
        if fairness_id:
            data.setdefault("extra_headers", {})["x-llm-d-inference-fairness-id"] = fairness_id
        return data


llmd_headers = LlmdHeaders()
