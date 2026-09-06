"""OCR transcription using Tesseract.

Provides a CLI for extracting text from images:
    python -m istota.skills.transcribe ocr /path/to/image.png
    python -m istota.skills.transcribe ocr /path/to/image.png --preprocess

The OCR itself lives in `istota.ocr_leaf` and is re-exported here. The daemon's
automatic attachment OCR spawns that leaf directly, because importing this
package runs `istota/skills/__init__.py` and star-imports every skill —
measured at 0.22s per spawn, on a pass that runs once per image. Keeping the
implementation in one place is what stops the hand-typed CLI and the spawned
child drifting apart; see the leaf's own docstring for the rule it keeps.
"""

import argparse

from istota.ocr_leaf import ocr_image, preprocess_image, text_from_data

from istota.skills._cli import run_skill_cli

__all__ = [
    "preprocess_image",
    "text_from_data",
    "cmd_ocr",
    "build_parser",
    "main",
]


def cmd_ocr(args) -> dict:
    """Run Tesseract OCR on an image file.

    A thin adapter over `istota.ocr_leaf.ocr_image`: this half owns the argparse
    namespace the skill CLI produces, the leaf owns the OCR.
    """
    return ocr_image(args.image_path, preprocess=args.preprocess)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.transcribe",
        description="OCR transcription skill",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ocr command
    ocr_parser = sub.add_parser("ocr", help="Extract text from image using OCR")
    ocr_parser.add_argument("image_path", help="Path to image file")
    ocr_parser.add_argument(
        "--preprocess",
        action="store_true",
        help="Apply preprocessing (grayscale + contrast) for better results",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "ocr": cmd_ocr,
    }

    run_skill_cli(commands, args)


if __name__ == "__main__":
    main()
