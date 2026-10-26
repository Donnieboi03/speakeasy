# Real-PDF fixtures: attribution

Each `doc.pdf` below is the arXiv PDF, committed **unmodified** (sha256 in `manifest.json` and in the spec's
`pdf_sha256`). Each is licensed under Creative Commons Attribution 4.0 International
(<https://creativecommons.org/licenses/by/4.0/>), as shown on its arXiv abstract page (checked 2026-10-26).
Ground truth (`gt.json`, `cases.json`) is agent-made with `gen/snap_gt.py` (`gt_source: "agent"`,
`reviewed_by_human: false`). The spoken scripts are short paraphrases or quotations of the papers, used only as test input.

| doc_id | arXiv id | Title | Authors | Licence |
|---|---|---|---|---|
| arx-2402.10153 | [2402.10153v2](https://arxiv.org/abs/2402.10153v2) | Knowledge-Infused LLM-Powered Conversational Health Agent: A Case Study for Diabetes Patients | Mahyar Abbasian, Zhongqi Yang, Elahe Khatibi, Pengfei Zhang, Nitish Nagesh, Iman Azimi, Ramesh Jain, Amir M. Rahmani | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), unmodified |
| arx-2405.08223 | [2405.08223v1](https://arxiv.org/abs/2405.08223v1) | An information-theoretic model of shallow and deep language comprehension | Jiaxuan Li, Richard Futrell | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), unmodified |
| arx-2406.06619 | [2406.06619v1](https://arxiv.org/abs/2406.06619v1) | LoRA-Whisper: Parameter-Efficient and Extensible Multilingual ASR | Zheshu Song, Jianheng Zhuo, Yifan Yang, Ziyang Ma, Shixiong Zhang, Xie Chen | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), unmodified |

The vendored font `tests/audit/bench/fonts/DejaVuSans.ttf` is DejaVu Sans 2.37 (Bitstream Vera derived, free licence in
`fonts/DejaVu-LICENSE.txt`).
