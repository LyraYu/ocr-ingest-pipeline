"""Base for errors caused by the input document (not by the pipeline).
`error_code` is the API error code the document is failed with (docs/DESIGN.md §9)."""


class PipelineInputError(Exception):
    error_code: str = "internal_server_error"
