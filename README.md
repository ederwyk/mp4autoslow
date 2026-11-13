# GoPro Auto Slow

Aplicativo desktop (Python + Tkinter + ffmpeg) que converte em lote os vídeos MP4 de uma pasta para **slow motion**, mantendo todos os quadros.

## Como funciona

1. Você seleciona uma pasta com arquivos `.mp4`.
2. O app lê o FPS e a duração de cada vídeo.
3. Você escolhe o FPS de saída (padrão **60**; também aceita digitar outro valor).
4. Cada vídeo é regravado a esse FPS **sem remover nenhum quadro**: os frames apenas são reposicionados no tempo, então a duração aumenta.

| FPS original | FPS de saída | Fator | Exemplo (vídeo de 10 s) |
|---|---|---|---|
| 240 | 60 | 4x | 40 s |
| 120 | 60 | 2x | 20 s |
| 60 | 60 | - | ignorado |
| 30 | 60 | - | ignorado |

Vídeos com FPS menor ou igual ao de saída são ignorados (não há o que desacelerar).

Os arquivos gerados ficam em `<pasta escolhida>/slowmo_<fps>fps/` com o nome `<original>_<fps>fps.mp4`. Os originais não são alterados.

### Detalhes técnicos

- Filtro de vídeo: `setpts=N/(FPS*TB)`, que atribui a cada quadro o timestamp `N/FPS`. Assim, nenhum quadro é descartado ou duplicado e a saída tem FPS constante (inclusive para fontes como 239,76 fps).
- Codec de saída: H.264 (`yuv420p`, `faststart`), compatível com qualquer player.
- Encoder (campo "Encoder"): no modo **Automático** o app testa a GPU ao abrir (NVIDIA NVENC, Intel QuickSync, AMD AMF no Windows; VideoToolbox no macOS) e a usa se funcionar, o que é bem mais rápido. Sem GPU compatível, usa `libx264 veryfast`. Há também "CPU alta qualidade" (`medium`, CRF 18), mais lento. Se a GPU falhar em um arquivo, ele é refeito automaticamente na CPU.
- Velocidade: como nenhum quadro é descartado, um vídeo de 240 fps gera 4x mais quadros a codificar do que um de 60 fps com a mesma duração; por isso a conversão é naturalmente pesada.
- Áudio: por padrão é **removido**. Com a opção "Manter áudio (desacelerado)", o áudio é esticado com `atempo` para acompanhar o vídeo (soará grave/lento).
- Trilhas de dados da GoPro (GPMF, timecode) não são copiadas; só o primeiro vídeo (e o primeiro áudio, se marcado).

## ffmpeg: viabilidade

É totalmente viável. O app usa o pacote [`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/), que traz um binário estático do ffmpeg para Windows e macOS (Intel e Apple Silicon). Com isso:

- O usuário final **não precisa instalar o ffmpeg**: o binário é embutido no executável.
- Não é necessário `ffprobe`: o FPS e a duração são lidos da saída de `ffmpeg -i`.
- Se o binário embutido não existir, o app tenta o `ffmpeg` do `PATH`.

Observação de licença: o ffmpeg distribuído inclui `libx264` (GPL). Considere isso ao redistribuir o executável.

## Executar a partir do código

Requer Python 3.9+ com Tkinter (já incluso nos instaladores oficiais do python.org; no Homebrew: `brew install python-tk`).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Gerar o executável

O PyInstaller não faz cross-compile: gere o Windows no Windows e o macOS no macOS.

```bash
pip install -r requirements-build.txt
python build.py
```

| Sistema | Resultado |
|---|---|
| Windows | `dist/GoProAutoSlow.exe` (arquivo único) |
| macOS | `dist/GoProAutoSlow.app` e `dist/GoProAutoSlow-macos.zip` |

Notas:

- **macOS**: o app gerado tem a arquitetura da máquina que o compilou (Apple Silicon ou Intel). Sem assinatura/notarização, o Gatekeeper pode bloquear; neste caso use clique direito > Abrir ou `xattr -cr GoProAutoSlow.app`.
- **Windows**: o SmartScreen pode alertar para executáveis não assinados.
- O workflow [.github/workflows/build.yml](.github/workflows/build.yml) gera os dois executáveis no GitHub Actions (manualmente ou ao criar uma tag `v*`).

## Estrutura

```
app.py                  # aplicativo (GUI + conversão)
build.py                # script de build com PyInstaller
requirements.txt        # dependências de execução
requirements-build.txt  # dependências de build
```
