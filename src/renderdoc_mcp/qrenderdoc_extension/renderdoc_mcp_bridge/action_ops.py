from .component import BridgeComponent


class ActionOps(BridgeComponent):
    def handlers(self):
        return {
            "list_actions": lambda params: self._list_actions(
                params.get("parent_event_id"),
                params.get("name_filter"),
                params.get("flags_filter"),
                params.get("cursor"),
                params.get("limit"),
            ),
            "search_actions": lambda params: self._search_actions(
                params.get("parent_event_id"),
                params.get("query"),
                params.get("flags_filter"),
                params.get("resource_id"),
                params.get("event_id_min"),
                params.get("event_id_max"),
                params.get("cursor"),
                params.get("limit"),
            ),
            "get_action_summary": lambda params: self._get_action_summary(int(params.get("event_id", 0))),
            "get_pipeline_overview": lambda params: self._get_pipeline_overview(int(params.get("event_id", 0))),
            "get_event_dossier": lambda params: self._get_event_dossier(
                int(params.get("event_id", 0)),
                params.get("binding_kinds", ["output_targets", "shaders"]),
                int(params.get("binding_limit", 20)),
            ),
            "get_event_dossiers": lambda params: self._get_event_dossiers(
                params.get("event_ids", []),
                params.get("binding_kinds", ["output_targets", "shaders"]),
                int(params.get("binding_limit", 20)),
            ),
            "list_pipeline_bindings": lambda params: self._list_pipeline_bindings(
                int(params.get("event_id", 0)),
                params.get("binding_kind", ""),
                params.get("cursor"),
                params.get("limit"),
            ),
            "get_shader_summary": lambda params: self._get_shader_summary(
                int(params.get("event_id", 0)),
                params.get("stage", ""),
            ),
            "get_shader_code_chunk": lambda params: self._get_shader_code_chunk(
                int(params.get("event_id", 0)),
                params.get("stage", ""),
                params.get("target"),
                params.get("start_line", 1),
                params.get("line_count", 200),
            ),
            "search_shader_code": lambda params: self._search_shader_code(
                int(params.get("event_id", 0)),
                params.get("stage", ""),
                params.get("target"),
                params.get("query", ""),
                bool(params.get("regex", False)),
                bool(params.get("case_sensitive", False)),
                int(params.get("context_lines", 2)),
                params.get("cursor"),
                params.get("limit"),
            ),
        }

    def _list_actions(self, parent_event_id, name_filter, flags_filter, cursor, limit):
        return self._call_bridge_client("_list_actions", parent_event_id, name_filter, flags_filter, cursor, limit)

    def _search_actions(self, parent_event_id, query, flags_filter, resource_id, event_id_min, event_id_max, cursor, limit):
        return self._call_bridge_client(
            "_search_actions",
            parent_event_id,
            query,
            flags_filter,
            resource_id,
            event_id_min,
            event_id_max,
            cursor,
            limit,
        )

    def _get_action_summary(self, event_id):
        return self._call_bridge_client("_get_action_summary", event_id)

    def _get_pipeline_overview(self, event_id):
        return self._call_bridge_client("_get_pipeline_overview", event_id)

    def _get_event_dossier(self, event_id, binding_kinds, binding_limit):
        return self._call_bridge_client("_get_event_dossier", event_id, binding_kinds, binding_limit)

    def _get_event_dossiers(self, event_ids, binding_kinds, binding_limit):
        return self._call_bridge_client("_get_event_dossiers", event_ids, binding_kinds, binding_limit)

    def _list_pipeline_bindings(self, event_id, binding_kind, cursor, limit):
        return self._call_bridge_client("_list_pipeline_bindings", event_id, binding_kind, cursor, limit)

    def _get_shader_summary(self, event_id, stage_name):
        return self._call_bridge_client("_get_shader_summary", event_id, stage_name)

    def _get_shader_code_chunk(self, event_id, stage_name, target, start_line, line_count):
        return self._call_bridge_client("_get_shader_code_chunk", event_id, stage_name, target, start_line, line_count)

    def _search_shader_code(self, event_id, stage_name, target, query, regex, case_sensitive, context_lines, cursor, limit):
        return self._call_bridge_client(
            "_search_shader_code",
            event_id,
            stage_name,
            target,
            query,
            regex,
            case_sensitive,
            context_lines,
            cursor,
            limit,
        )
