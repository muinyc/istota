"""Whisper transcription skill CLI.

CLI:
    python -m istota.skills.whisper transcribe /path/to/audio.wav [--model auto] [--language en]
    python -m istota.skills.whisper transcribe /path/to/audio.wav --output srt --save
    python -m istota.skills.whisper models
    python -m istota.skills.whisper download <model-name>
"""

import argparse
import json
from pathlib import Path

from istota.skills._cli import run_skill_cli
from istota.skills.whisper.models import (
    download_model,
    get_available_memory_gb,
    list_models,
)
from istota.skills.whisper.transcribe import (
    format_srt,
    format_vtt,
    transcribe_audio,
)


def cmd_transcribe(args) -> dict:
    """Transcribe an audio file."""
    result = transcribe_audio(
        args.audio_path,
        model=args.model,
        language=args.language,
    )

    if result.get("status") == "error":
        return result

    output_format = args.output

    if output_format == "text":
        text = result["text"]
        if args.save:
            out_path = Path(args.audio_path).with_suffix(".txt")
            out_path.write_text(text)
            result["saved_to"] = str(out_path)
        if args.no_segments:
            result.pop("segments", None)
        return result

    if output_format == "srt":
        formatted = format_srt(result["segments"])
        if args.save:
            out_path = Path(args.audio_path).with_suffix(".srt")
            out_path.write_text(formatted)
            result["saved_to"] = str(out_path)
        result["formatted_output"] = formatted
        del result["segments"]
        return result

    if output_format == "vtt":
        formatted = format_vtt(result["segments"])
        if args.save:
            out_path = Path(args.audio_path).with_suffix(".vtt")
            out_path.write_text(formatted)
            result["saved_to"] = str(out_path)
        result["formatted_output"] = formatted
        del result["segments"]
        return result

    # json (default) — return full result with segments
    if args.save:
        out_path = Path(args.audio_path).with_suffix(".json")
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        result["saved_to"] = str(out_path)

    # After the save, so `--save` still writes the whole thing. `segments` is
    # one entry per *word*, which is megabytes on a long recording; a caller
    # that only wants the transcript should not have to receive it, parse it
    # and drop it (ISSUE-273).
    if args.no_segments:
        result.pop("segments", None)

    return result


def cmd_models(args) -> dict:
    """List available models."""
    models = list_models()
    available_gb = get_available_memory_gb()
    return {
        "status": "ok",
        "available_memory_gb": round(available_gb, 1),
        "models": models,
    }


def cmd_download(args) -> dict:
    """Download a model."""
    return download_model(args.model_name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.whisper",
        description="Audio transcription using faster-whisper (CPU, int8)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # transcribe command
    tr = sub.add_parser("transcribe", help="Transcribe an audio file")
    tr.add_argument("audio_path", help="Path to audio file")
    tr.add_argument(
        "--model",
        default="auto",
        help="Model name or 'auto' to select based on available RAM (default: auto)",
    )
    tr.add_argument("--language", help="Language code (e.g., 'en'). Auto-detected if omitted.")
    tr.add_argument(
        "--output",
        choices=["json", "text", "srt", "vtt"],
        default="json",
        help="Output format (default: json)",
    )
    tr.add_argument(
        "--save",
        action="store_true",
        help="Save output to file alongside the audio file",
    )
    tr.add_argument(
        "--no-segments",
        action="store_true",
        help="Omit per-word segments from json/text output (transcript text only)",
    )

    # models command
    sub.add_parser("models", help="List available models and RAM requirements")

    # download command
    dl = sub.add_parser("download", help="Pre-download a model")
    dl.add_argument("model_name", help="Model to download (e.g., 'small', 'medium')")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "transcribe": cmd_transcribe,
        "models": cmd_models,
        "download": cmd_download,
    }

    run_skill_cli(commands, args)
