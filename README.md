[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Notion API](https://img.shields.io/badge/Notion_API-000?logo=notion&logoColor=white)](https://developers.notion.com)
[![Demo](https://img.shields.io/badge/Demo-Notion_Dashboard-blue)](https://walnut-cobra-1eb.notion.site)

# Podcast Digest Pipeline

A CLI pipeline that turns a podcast URL into a structured Notion knowledge base entry — with phrase extraction, cross-episode synthesis, and human eval tracking.

## 📺 Demo

<p align="center">
  <img src="docs/podcast-pipeline.gif" alt="Podcast Pipeline Demo" width="100%" />
</p>

**Watch it in action:** Paste a podcast URL → Pipeline extracts key insights → Writes to Notion database in gallery view

## What it does

- 🎙️ **Extract from audio** — Parse podcast transcripts/metadata
- 🔍 **Phrase extraction** — Pull key moments and themes  
- 🔗 **Cross-episode synthesis** — Link related topics across episodes
- 📊 **Notion integration** — Auto-populate structured database
- ✅ **Human eval tracking** — Mark quality/usefulness scores

## Quick Start

```bash
pip install -r requirements.txt
python main.py --url "https://podcasts.example.com/episode"
```

## Output

Results automatically write to your Notion database:
- Episode metadata
- Key phrases & timestamps
- Synthesis notes
- Evaluation scores

## Tech Stack

- Python 3.8+
- Notion API
- Audio processing libraries
- JSON output

## License

MIT - See [LICENSE](LICENSE) for details

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

Made with ❤️ by Yingshi Liu
