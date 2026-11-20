# Optional tsqyomi v5 Japanese context-reading model

Source: https://huggingface.co/tsukumijima/tsqyomi-models

Pinned revision: `81add61ddba9669d328e307c883d05d77d60f5f4`, directory `v5/`.
The unchanged ONNX model, tokenizer and metadata hashes are recorded in
`config/japanese_reading.py` and in the generated pack provenance.

The upstream model card declares `license: mit` and `library_name: onnxruntime`.
It identifies these files as internal model assets for pyopenjtalk-plus and asks
consumers to use that library. Amadeus uses its native candidate-selection and
NJD APIs. The repository's `LICENSE.txt` supplies the full MIT notice, including
`Copyright (c) 2026 Aivis Project`; `MIT.txt` retains that notice verbatim. This
record relies on the immediate distributor's explicit model license declaration;
it does not establish the exact training-data provenance.

Model-card permalink:
https://huggingface.co/tsukumijima/tsqyomi-models/blob/81add61ddba9669d328e307c883d05d77d60f5f4/README.md

License permalink:
https://huggingface.co/tsukumijima/tsqyomi-models/blob/81add61ddba9669d328e307c883d05d77d60f5f4/LICENSE.txt

Upstream frontend: https://github.com/tsukumijima/pyopenjtalk-plus

The optional `japanese-context-reading` pack contains only the three unchanged
model assets, provenance, this notice and the MIT terms. No voice recordings,
personal dictionary or executable Python code is bundled. Startup never
downloads assets. This is a contextual reading selector, not an emotion planner
or a guarantee of correct readings for every Japanese sentence.
