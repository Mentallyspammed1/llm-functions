class EditOptions(dict):
    """Simple dict-subclass that supports attribute access with defaults."""
    
    DEFAULTS = {
        "operation": None,
        "file_path": None,
        "target_path": None,
        "content": None,
        "search_text": None,
        "replacement": None,
        "pattern": None,
        "use_regex": False,
        "global_replace": True,
        "case_sensitive": True,
        "line_number": None,
        "start_line": None,
        "end_line": None,
        "encoding": "utf-8",
        "max_size": 10485760,
        "max_write_size": 104857600,
        "show_lines": True,
        "add_newline": True,
        "create_parents": True,
        "preserve_metadata": True,
        "include_hidden": False,
        "sort_by": "name",
        "descending": False,
        "parents": True,
        "recursive": False,
        "line_context": 0,
        "context_lines": 3,
        "max_matches": 1000,
        "truncate_size": 0,
        "max_backups": 15,
        "mode": None,
        "to_type": None,
        "backup_timestamp": None,
        "algorithm": "sha256",
        "n_lines": 10,
        "compare_mode": "bytes",
        "compression": "deflate",
        "password": None,
        "variables": {},
        "undefined_var": "error",
        "file_pattern": "*",
        "min_size": None,
        "max_size_filter": None,
        "modified_after": None,
        "modified_before": None,
        "file_type": "any",
        "max_results": 10000,
        "ops": [],
        "edits": [],
        "continue_on_error": False,
        "dry_run": False
    }

    def __init__(self, **kwargs):
        # Merge defaults with provided kwargs
        data = self.DEFAULTS.copy()
        data.update(kwargs)
        super().__init__(data)

    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__
