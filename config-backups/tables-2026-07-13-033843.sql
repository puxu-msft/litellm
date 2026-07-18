--
-- PostgreSQL database dump
--

\restrict SefIoIOt0tvBbokGahh3odCnUP2hVAi0dvMJGSWHRVKTW11XePjdfEwSP8olt6h

-- Dumped from database version 18.4 (Homebrew)
-- Dumped by pg_dump version 18.4 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: LiteLLM_Config; Type: TABLE; Schema: public; Owner: xp
--

CREATE TABLE public."LiteLLM_Config" (
    param_name text NOT NULL,
    param_value jsonb
);


ALTER TABLE public."LiteLLM_Config" OWNER TO xp;

--
-- Name: LiteLLM_ProxyModelTable; Type: TABLE; Schema: public; Owner: xp
--

CREATE TABLE public."LiteLLM_ProxyModelTable" (
    model_id text NOT NULL,
    model_name text NOT NULL,
    litellm_params jsonb NOT NULL,
    model_info jsonb,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    created_by text NOT NULL,
    updated_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_by text NOT NULL,
    blocked boolean DEFAULT false NOT NULL
);


ALTER TABLE public."LiteLLM_ProxyModelTable" OWNER TO xp;

--
-- Data for Name: LiteLLM_Config; Type: TABLE DATA; Schema: public; Owner: xp
--

COPY public."LiteLLM_Config" (param_name, param_value) FROM stdin;
model_cost_map_reload_config	{"force_reload": false, "interval_hours": null}
general_settings	{"ui_access_mode": "all", "health_check_interval": 300, "enable_public_model_hub": false, "database_connection_timeout": 60, "store_prompts_in_spend_logs": true, "database_connection_pool_limit": 10, "health_check_skip_disabled_background_models": false}
router_settings	{"model_group_alias": {"gpt": "github_copilot/gpt-5.6-sol", "opus": "github_copilot/claude-opus-4.8", "haiku": "github_copilot/claude-haiku-4.5", "sonnet": "github_copilot/claude-sonnet-5", "gpt-5.5": "github_copilot/gpt-5.6-sol", "claude-opus-4-8": "github_copilot/claude-opus-4.8", "claude-opus-4.8": "github_copilot/claude-opus-4.8", "claude-sonnet-5": "github_copilot/claude-sonnet-5", "claude-haiku-4-5": "github_copilot/claude-haiku-4.5", "claude-haiku-4.5": "github_copilot/claude-haiku-4.5", "claude-haiku-4-5-20251001": "github_copilot/claude-haiku-4.5"}}
\.


--
-- Data for Name: LiteLLM_ProxyModelTable; Type: TABLE DATA; Schema: public; Owner: xp
--

COPY public."LiteLLM_ProxyModelTable" (model_id, model_name, litellm_params, model_info, created_at, created_by, updated_at, updated_by, blocked) FROM stdin;
34decbf8-ff47-4423-878e-19c80d836fdc	github_copilot/claude-opus-4.8	{"tags": [], "model": "LlqvWN8jz6qtj13HW-gr-MSw0uVvk3SBX1KyzLW4AX1eEAuMMbrEPPVrO4Vz9JFMuffzytzaUWorBKOdyAx9dOnA9I8wlg==", "api_base": "zTrn7PVJ4vt-vqZblxDyT1b_aHSj0OUpj1DXZalkIKHCnbkW7ZmLGWeBnHq1XDcV1L0pacx8HlGo6vpgEhr8GhnC4qWnswg-M2l0u6b9GJo=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "RsBJUIETuvyCix5U9IkStpdWU5M4PWAtRfWXEGMrWiPKx20VpJ0F8cxOolva3h5UhKiGUcit", "use_in_pass_through": false, "input_cost_per_token": 0.0005, "output_cost_per_token": 0.0025, "litellm_credential_name": "eAOf9XAhI1eZJuSg7MeR9aMplinjf50ejZr5fhr3SvGoJF4yRy2gpPcfOXonVpx0", "merge_reasoning_content_in_choices": false}	{"id": "34decbf8-ff47-4423-878e-19c80d836fdc", "key": "github_copilot/claude-opus-4.8", "mode": "chat", "blocked": false, "db_model": true, "access_groups": [], "direct_access": true, "litellm_provider": "github_copilot", "access_via_team_ids": [], "input_cost_per_token": 0.0005, "output_cost_per_token": 0.0025, "supported_openai_params": ["frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "modalities", "prediction", "n", "presence_penalty", "seed", "stop", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "audio", "web_search_options", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format"]}	2026-07-11 23:36:30.382	default_user_id	2026-07-12 18:56:27.724	default_user_id	f
6ad92d77-6c04-4cfd-8d9e-7e40a2eb2754	github_copilot/claude-sonnet-5	{"tags": [], "model": "182hLus4RAfc7Ow98AtUSZOxHfb5y1TteyumKZjq3ljFGN7ZXEq49-CkiJhPP3vbtC2e3aDDZSXXxQp40z1_ZThwu5t9lw==", "api_key": "O0mPvM7QLWdmmsdv2_73Pd13HtSP4X6PYLXTHYr9An0niPRokAuQ1yCaRU0eWoNg6Nuca6ZuizxZlY1PK2XBTQGwiOXezr9JL-yO1nbSlXOKqg==", "api_base": "jIH0xGECNk5j5R5yU8mKIih1miiQl5sw2aN6GU4FU4-fNWx5-RdH-4u01eTUBqdAl28BvX4u55jLw3AYB3mQNHszsdKSYNYLkorm94Q5EgA=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "4BCsnvaLKC91k18eEdECV_V_ojXuiE25o5VraS1hHgUpCb_RBXcLfS1xl_0y0pGsIHK4UIMw", "use_in_pass_through": false, "input_cost_per_token": 0.0002, "output_cost_per_token": 0.001, "litellm_credential_name": "dmAKUABLVCHOoijrrhKyeZhCm4c7qSpqV-2K0yRSnHM66yLqfeOjiQzBiUwRdLxe", "cache_read_input_token_cost": 0.00002, "cache_creation_input_token_cost": 0.00025, "merge_reasoning_content_in_choices": false}	{"id": "6ad92d77-6c04-4cfd-8d9e-7e40a2eb2754", "key": "github_copilot/claude-sonnet-5", "mode": "chat", "blocked": false, "db_model": true, "access_groups": [], "direct_access": true, "litellm_provider": "github_copilot", "access_via_team_ids": [], "input_cost_per_token": 0.0002, "output_cost_per_token": 0.001, "supported_openai_params": ["frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "modalities", "prediction", "n", "presence_penalty", "seed", "stop", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "audio", "web_search_options", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format", "thinking", "reasoning_effort"], "cache_read_input_token_cost": 0.00002, "cache_creation_input_token_cost": 0.00025}	2026-07-11 23:20:31.233	default_user_id	2026-07-12 18:56:44.273	default_user_id	f
06209bf1-4b19-41f2-93a7-9020c24b7823	haiku	{"tags": [], "model": "urOcHJdBobC4eDMj7FwECUJByFnrodiFOgu92FuBUy1tJih35zvyZ8F0SGE2RsjWWm5hSq7bExETSa_rlHi_6wwmGmfAgLU=", "api_base": "xTDze0w_FETwS8vU3PCawgjgVA46OOfxTob-XWRoDNYzcWzwBlWlhOE3LG_aNgLXi9MoagzNgKG3BlnGsAOBn3wTzoml3usko4O3cPe2cGc=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "9jsx4CB1OnTbWYyw4lzZAbUmZsSu_m1Xe-7tXHWlhE4rQDJ2XU2KyDtcUtPd_s9NtsI75Xzv", "use_in_pass_through": false, "input_cost_per_token": 0.0001, "output_cost_per_token": 0.0005, "litellm_credential_name": "IR8_NIwdtp8EfgnMgZkXXX-hRyF1TpHmz3pBC9_tQK8luzuSdKCEEuJbMLHKKtDf", "merge_reasoning_content_in_choices": false}	{"id": "06209bf1-4b19-41f2-93a7-9020c24b7823", "key": "github_copilot/claude-haiku-4.5", "mode": "chat", "blocked": false, "db_model": true, "max_tokens": 16000, "access_groups": [], "direct_access": true, "supports_vision": true, "litellm_provider": "github_copilot", "max_input_tokens": 128000, "max_output_tokens": 16000, "access_via_team_ids": [], "input_cost_per_token": 0, "output_cost_per_token": 0, "supported_openai_params": ["frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "modalities", "prediction", "n", "presence_penalty", "seed", "stop", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "audio", "web_search_options", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format"], "supports_function_calling": true}	2026-07-12 00:11:58.556	default_user_id	2026-07-12 18:57:59.626	default_user_id	t
33c036ef-75ec-4c1b-98fa-61ca04e7e52f	sonnet	{"tags": [], "model": "_l6YiphizOs0EZjZ4lhL_yxadtc709JQbZECe4Bxf_XOmMoMb3eLgiD1az4P3XZM9O2DQuCQ0E3-nYavmG-GcVGDsNF10w==", "api_base": "hsgsRhxqWhJ79nPNlyhgPQe8teG7qc0S_0mdyeFjYhcvCfQgxpU4xRaZi6Ew5qkJ3HwM3DlppL3Vwo1_t92cPeUpPfZKGrt9wE92xkdWSL4=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "gS5c67w8OHBAZaUa7w-kAi1Op4u3Qqi-yArY4-9UdpI_AwVPRCTq67e4P80Us2UuLnzwLW0I", "use_in_pass_through": false, "input_cost_per_token": 0.0002, "output_cost_per_token": 0.001, "litellm_credential_name": "v6jPJbxBBmmNaPC2SV8rzyU__kJ_c0U_1YVvBJH3jJm1Y8kWMfpvQNUhfJGIlV4R", "cache_read_input_token_cost": 0.00002, "cache_creation_input_token_cost": 0.00025, "merge_reasoning_content_in_choices": false}	{"id": "33c036ef-75ec-4c1b-98fa-61ca04e7e52f", "blocked": false, "db_model": true, "access_groups": [], "direct_access": true, "access_via_team_ids": []}	2026-07-12 00:11:43.758	default_user_id	2026-07-12 18:58:00.828	default_user_id	t
55edee73-5e5a-48bc-b2b8-73e384807d43	github_copilot/gpt-*	{"tags": [], "model": "5OrhcUlZdE0P40rY5_FV7rjcRSlKNKEO-bFl55w96FlzlVlORsVxYKo65Z1HFabj4L_an1i9H34porII", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "W9mFmjNP7HbLusdpAw5tvgLRy-BjP8uBIUuWn7y0mEwZkNME6vAUV3LDDzunobEt8biTyJiS", "use_in_pass_through": false, "litellm_credential_name": "xyfjRv-JxGFNFXre7JXeWlAH3FBqr7dyFuElyMYml6gfUt7ror7y7iJf1In8JRa1", "merge_reasoning_content_in_choices": false}	{"id": "55edee73-5e5a-48bc-b2b8-73e384807d43", "blocked": false, "db_model": true, "access_groups": [], "direct_access": true, "access_via_team_ids": []}	2026-07-12 00:00:27.479	default_user_id	2026-07-12 18:49:44.593	default_user_id	t
32eef3a1-d6f0-4452-bc97-b2798020c7b3	opus	{"tags": [], "model": "1IA5oeCc-B4Kxa0Ntz1TPji1AsYtHpuOfT37R_Or8DvCPPSd8F1Q3RDidFXthX6ijNwETBUMSMPxUMhwx7RAKrY0YSgaEg==", "api_base": "UBkux_nScMaa_g09U0kxd1K1HRJc3qlEYg2RXBr0Mm9AlUVdljS5BiZcKeQ_nqwDJmfW5Y0r5d5GkLZ7FT_47DzpXjj-7Opn0hG0_BZ8Sac=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "Xos23b72VdcQg8YBcIO7xUVU09SJh6vDQirLB2PhUUyOuJhAKPeb6oqNoGduKZV0-TqYYHzt", "use_in_pass_through": false, "input_cost_per_token": 0.0005, "output_cost_per_token": 0.0025, "litellm_credential_name": "mrLMIbFcPMi79X8lyaM-fOZupGDLkEogJXCyaB2vKNaNy4M4xjufCLBEmbaqKMyI", "merge_reasoning_content_in_choices": false}	{"id": "32eef3a1-d6f0-4452-bc97-b2798020c7b3", "key": "github_copilot/claude-opus-4.8", "mode": "chat", "blocked": false, "db_model": true, "access_groups": [], "direct_access": true, "litellm_provider": "github_copilot", "access_via_team_ids": [], "input_cost_per_token": 0, "output_cost_per_token": 0, "supported_openai_params": ["frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "modalities", "prediction", "n", "presence_penalty", "seed", "stop", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "audio", "web_search_options", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format"]}	2026-07-12 00:11:22.592	default_user_id	2026-07-12 18:58:02.134	default_user_id	t
daab3f1e-efcf-49f7-b75a-d8bbee004630	claude-haiku-4-5	{"tags": [], "model": "rPR2lyqhyWuVC3TTADgZrT0bsjCaZ8XPrc0HbFPU6bKGuNDj44fYW0VztEIVs7suBwVKicP6f7znQKIcwu2jzxPSucrpAdc=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "jGBk3HImNPkLXAXEfS3aAmAH32yUB6i7Xd3xMAkAcqH4yi0-ySZlWY45qY8nUSn4SUScpX7d", "use_in_pass_through": false, "litellm_credential_name": "dyeYLEs_Vc1tavCI_V9paY_B1H7BS5RwCFPUiX3SmEOTpvgK7tlzUt2fnFAwJ24V", "merge_reasoning_content_in_choices": false}	{"id": "daab3f1e-efcf-49f7-b75a-d8bbee004630", "key": "github_copilot/claude-haiku-4.5", "mode": "chat", "blocked": false, "db_model": true, "max_tokens": 16000, "access_groups": [], "direct_access": true, "supports_vision": true, "litellm_provider": "github_copilot", "max_input_tokens": 128000, "max_output_tokens": 16000, "access_via_team_ids": [], "input_cost_per_token": 0.0001, "output_cost_per_token": 0.0005, "supported_openai_params": ["frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "modalities", "prediction", "n", "presence_penalty", "seed", "stop", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "audio", "web_search_options", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format"], "supports_function_calling": true}	2026-07-12 12:49:48.317	default_user_id	2026-07-12 18:57:36.104	default_user_id	t
9477544f-4da0-43b7-bd86-2da317a5e774	haiku	{"model": "f5A4GaFzm-UcJVyCFPvoi8-c3YWjvxVqM5pacXEu4_no-rrLu8AFSzx9xYMVlRaPYaiU7ibSXYq4vqtelP_NEO6EDP9vJyo=", "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "3134jdLBj-Di5s5Pmww6_J-7HARsotzbGd-lOLDuko1MA126kMBDVEnQQr2z2dslwF-LsgP5", "use_in_pass_through": false, "litellm_credential_name": "Wk4chPgLEZIpwVgVzbNxOnFIsXCYJWKyjvavIOLT4Y3TH7cDvC_ebY-Oh8vXzFkP", "merge_reasoning_content_in_choices": false}	{"id": "9477544f-4da0-43b7-bd86-2da317a5e774", "mode": "chat", "db_model": false}	2026-07-12 12:50:04.817	default_user_id	2026-07-12 18:57:37.115	default_user_id	t
24c5be56-6662-4e8f-a948-e5300f244e9a	github_copilot/claude-haiku-4.5	{"tags": [], "model": "oIbwU16fn6WgWln38O9_asy7kAb9QJqqc-05IkXGusvq5UVOMbfjjSCdc_07aFgGbczDwBmRRRB8QJ1_6o6fltAlMwLW2co=", "api_key": "gXydzQZfk9AH9KH8RkC34Ffgj06ehbuVdElCvWhorT200yRp8h_RwrZVoaWYHG6l8mcZ2hL2Ui0qefeJCRPtrutSm4FRPIAQCfyuOWY2MQzLNg==", "api_base": "aMY_8tshUTBnr4mjvu4IotgRJsEJk4DlxGy23R-UnHs_vWv5ADSdV_0mFQQ4Parvl6tGYwncXcd5V9bXRIcfU2JRJ8y_lJvD24Yfkk25-iQ=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "Sn-JNuyOaOx-T6KuaY7jSNP1pTfX3KwpJn_Ej56k-mZjVw6RdkPQvAIEUR_EzgETIGj9-SUD", "use_in_pass_through": false, "input_cost_per_token": 0.0001, "output_cost_per_token": 0.0005, "litellm_credential_name": "U3mZjkVjZDp05W6R9AwOowWoz3Ny7976EfFVE2lXIqA3dUQdaI5PwfK9RSDBvG1D", "merge_reasoning_content_in_choices": false}	{"id": "24c5be56-6662-4e8f-a948-e5300f244e9a", "key": "github_copilot/claude-haiku-4.5", "mode": "chat", "blocked": false, "db_model": true, "max_tokens": 16000, "access_groups": [], "direct_access": true, "supports_vision": true, "litellm_provider": "github_copilot", "max_input_tokens": 128000, "max_output_tokens": 16000, "access_via_team_ids": [], "input_cost_per_token": 0.0001, "output_cost_per_token": 0.0005, "supported_openai_params": ["frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "modalities", "prediction", "n", "presence_penalty", "seed", "stop", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "audio", "web_search_options", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format"], "supports_function_calling": true}	2026-07-11 23:20:31.167	default_user_id	2026-07-12 18:57:14.618	default_user_id	f
7066537e-fb7b-491b-82dd-51da7e487776	github_copilot/gpt-5.6-sol	{"tags": [], "model": "aOnliMs27Q_rKHBzVepV4B9JcYIMjz0CGaPCWP0hZMpdl6pMa-xVuPtA_krWTDR7SuwPK5lbaLaPIlxKfg6ta5yh", "api_base": "aNwpNi8LiTGcOTFKel1KywpCdV88KxLf3sspi84N2JJ9niBE_aKmronuuF0p9RLrnaNqYWZKknGk0T1Hp5_0EJ8VSOGNzChPWOBrHFUoNAA=", "guardrails": [], "use_xai_oauth": false, "use_litellm_proxy": false, "custom_llm_provider": "Nxe-13cCrzoIk_hqgnWNntHwCwHCjBG9IgCLEnZAwDxlJExHNbw3z5EjKjGrqG4rKHP22hPR", "use_in_pass_through": false, "input_cost_per_token": 0.0005, "output_cost_per_token": 0.003, "litellm_credential_name": "DKWOLoHXcd5e1zA5eqWvSZSLLIabSHOvf2DT8a0DzAx__UZk6J2uA_Fa97czkWqf", "merge_reasoning_content_in_choices": false}	{"id": "7066537e-fb7b-491b-82dd-51da7e487776", "key": "github_copilot/gpt-5.5", "mode": "responses", "blocked": false, "db_model": true, "access_groups": [], "direct_access": true, "litellm_provider": "github_copilot", "access_via_team_ids": [], "supported_endpoints": ["/responses"], "input_cost_per_token": 0.0005, "output_cost_per_token": 0.003, "supported_openai_params": ["logprobs", "top_logprobs", "max_tokens", "max_completion_tokens", "n", "seed", "stream", "stream_options", "temperature", "top_p", "tools", "tool_choice", "function_call", "functions", "max_retries", "extra_headers", "parallel_tool_calls", "service_tier", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "store", "response_format", "reasoning_effort", "verbosity"]}	2026-07-11 23:43:12.605	default_user_id	2026-07-12 18:57:47.855	default_user_id	f
\.


--
-- Name: LiteLLM_Config LiteLLM_Config_pkey; Type: CONSTRAINT; Schema: public; Owner: xp
--

ALTER TABLE ONLY public."LiteLLM_Config"
    ADD CONSTRAINT "LiteLLM_Config_pkey" PRIMARY KEY (param_name);


--
-- Name: LiteLLM_ProxyModelTable LiteLLM_ProxyModelTable_pkey; Type: CONSTRAINT; Schema: public; Owner: xp
--

ALTER TABLE ONLY public."LiteLLM_ProxyModelTable"
    ADD CONSTRAINT "LiteLLM_ProxyModelTable_pkey" PRIMARY KEY (model_id);


--
-- PostgreSQL database dump complete
--

\unrestrict SefIoIOt0tvBbokGahh3odCnUP2hVAi0dvMJGSWHRVKTW11XePjdfEwSP8olt6h

