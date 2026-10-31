# Third-party notices

## Zhihu Open Platform CLI-compatible runtime

`scripts/zhihu_adapter.py` invokes a separately installed Zhihu Open Platform CLI-compatible runtime as an external precompiled dependency. The binary and its source code are not distributed inside this Skill, and this repository does not independently verify its vendor provenance.

Users must identify the runtime's distributor and recheck its applicable license, service terms, redistribution rights, and version-specific notices. This notice is not a license grant for that binary.

## yt-search-download

The YouTube search/filter behavior in `scripts/youtube_search.py` is derived in part from [joeseesun/yt-search-download](https://github.com/joeseesun/yt-search-download).

MIT License

Copyright (c) 2026 Joe Sun (@joeseesun)

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## AI HOT Skill output format

`references/aihot-output-format.md` is copied from `references/output-format.md` of the `aihot` Skill published by AI HOT (aihot.virxact.com) and included in [KKKKhazix/khazix-skills](https://github.com/KKKKhazix/khazix-skills/tree/main/aihot) under the MIT License, Copyright (c) 2026 数字生命卡兹克. The complete license text is kept in the repository's `licenses/KKKKhazix-khazix-skills-LICENSE.txt`. Only a source note was added; `scripts/aihot_search.py` is an independent implementation of the public AI HOT API calls and does not include upstream code.
