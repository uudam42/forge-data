import warnings

# Several response/manifest models (ValidationResponse, ValidationReport,
# IntegrityResponse, NormalizationResponse, NormalizationManifest,
# StreamLineage) have a field literally named `schema` -- a real, shipped
# wire/manifest field (see e.g. app/validation/models.py) that predates
# this project's use of Pydantic v2 and must not be renamed: it would
# break the on-disk manifest format and the HTTP API contract for
# existing workspaces. Pydantic v2 warns because this shadows
# BaseModel's own deprecated `.schema()` classmethod -- a purely
# cosmetic collision (nothing in this codebase calls `.schema()` on
# these models) that would otherwise print on every fresh process
# import of these modules, including every `forge` CLI invocation.
warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema" in ".*" shadows an attribute in parent "BaseModel"',
    category=UserWarning,
)
