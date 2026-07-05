from core.document.manifest import sha256_text
from core.models.chunk import TextChunk
from core.models.validation import ValidationReport


def validate_chunk_reconstruction(
    project_id: str, source_text: str, chunks: list[TextChunk]
) -> ValidationReport:
    reconstructed = "".join(chunk.text for chunk in chunks)
    errors: list[str] = []

    if reconstructed != source_text:
        errors.append("Concatenated chunk text does not equal source text.")

    expected_start = 0
    for chunk in chunks:
        if chunk.source_span.start != expected_start:
            errors.append(
                f"{chunk.chunk_id} starts at {chunk.source_span.start}, "
                f"expected {expected_start}."
            )
        if chunk.source_span.end < chunk.source_span.start:
            errors.append(f"{chunk.chunk_id} has an invalid source span.")
        expected_start = chunk.source_span.end

    if expected_start != len(source_text):
        errors.append(f"Final chunk ends at {expected_start}, expected {len(source_text)}.")

    return ValidationReport(
        project_id=project_id,
        exact_reconstruction_success=not errors,
        chunk_count=len(chunks),
        source_character_count=len(source_text),
        reconstructed_character_count=len(reconstructed),
        source_hash=sha256_text(source_text),
        reconstructed_hash=sha256_text(reconstructed),
        errors=errors,
    )
