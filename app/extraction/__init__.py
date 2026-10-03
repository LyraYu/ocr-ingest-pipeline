from app.errors import PipelineInputError


class UnsupportedDocumentTypeError(PipelineInputError):
    """The document text matches none of the supported document types."""

    error_code = "unsupported_document_type"
