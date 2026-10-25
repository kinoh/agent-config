---
name: herdr-watch
description: Herdr 内（HERDR_ENV=1）で、終了まで30秒を超えるコマンド・CI run・全件テスト・デプロイ・リモート処理・外部状態の変化を待つとき、または別ペインのエージェントへ作業を委託するときに使う。待機を別ペインの監視スクリプトに任せる、または委託先から返信させることで自分のターンを終え、届いたメッセージで再開する。sleep・wait・ログの繰り返し読み取り・`herdr agent prompt --wait`・`herdr agent wait` で待つ代わりに使う
---

# Herdr Watch

エージェントは、ツールを呼ぶたびに会話全体を読み直す。sleep やログの再読で待つと、待っている間も1回ごとに会話全体のトークンを消費する
待機は別ペインの監視スクリプトに任せ、委託した作業は委託先から返信させて、自分はターンを終える。対象が終わると呼び出し元のエージェントへ結果の場所が届くので、そこから作業を再開する

Herdr の操作そのものは herdr skill に従う。ただし長い処理や委託先の完了を待つときは、herdr skill にある `--wait` や `agent wait` ではなく、この skill を使う

## 使う場面

- 終了まで30秒を超える処理を待つとき。例: GitHub Actions の run、全件テスト、デプロイ、リモートのジョブ、外部状態の変化
- 別ペインのエージェントへ作業を委託し、その結果を受け取るとき
- 30秒以内に終わる見込みの非対話コマンドは、初回30秒待って直接実行する。30秒後も継続していたら、下の「直接実行から監視へ切り替える」に従う
- `HERDR_ENV=1` でなければ使わない

本文の `<このskillのディレクトリ>` は、この `SKILL.md` が置かれた実際の絶対パスに置き換える。ローカル配置・plugin cache のどちらでも、同梱の `scripts/herdr_watch.py` を使う

## 監視を始める

完了するまで戻らないコマンドを渡す。そのコマンドが終了した時点で待機が終わる

```bash
python3 "<このskillのディレクトリ>/scripts/herdr_watch.py" start --label "<何を待つか>" [--timeout <秒>] [--cwd <dir>] -- <command> [args...]
```

完了するまで戻らないコマンドの例:

- GitHub Actions: `-- gh run watch <run-id> --exit-status`
- 状態の変化: `-- bash -c 'until <完了条件のコマンド>; do sleep 60; done'`
- 長いテスト: `-- npm test`

コマンドの扱い:

- コマンドは監視ペインのシェルで、ユーザーの権限で動く。エージェントの sandbox や承認は掛からない。そのため、自分で直接実行してよいコマンドに限る
- `--timeout` は、上限が分かっている場合だけ指定する。指定しなければ、コマンドが終わるまで待つ。上限を超えたら監視スクリプトがコマンドを止め、`timed_out` として通知する
- `--cwd` を省くと、呼び出し元の作業ディレクトリで実行する

`start` は監視ペインを開くとすぐに戻り、JSON で `run_dir`・`watcher_pane`・`caller_pane` を返す。失敗した場合は理由を stderr に出して終了コード1で終わる

## 直接実行から監視へ切り替える

非対話コマンドを直接実行するときは、初回の待機時間を30秒にする。Codex の `exec_command` では `yield_time_ms: 30000` を指定する。コードモードの `exec` で包む場合は、外側に `// @exec: {"yield_time_ms": 60000}` を指定して、内側の待機より先に返さない

30秒を超えると見込める処理は、最初から上の `start` で実行する。直接実行が30秒後も継続していた場合は、コマンドを起動し直さず、既存プロセスの終了だけを監視する

1. 元のツールの実行識別子を控える。Codex の `exec_command` が返す `session_id` は OS の PID ではない
2. 起動したコマンド・親子関係・作業ディレクトリ・開始時刻を照合して、その実行に対応する OS の PID を一度特定する。`/proc/<PID>/stat` の22番目のフィールド（開始時刻）も控える。別のプロセスや子プロセスの一部だけを対象にしない。PID を一意に特定できなければ、その理由を報告して止める
3. 次のコマンドで、その PID と開始時刻に対応する実行の終了を待つ。この監視はプロセスを操作せず、Linux の pidfd で終了を待つ

```bash
python3 "<このskillのディレクトリ>/scripts/herdr_watch.py" start --label "既存実行 <元の実行識別子> の終了待ち" -- python3 -c '
import os
import select
import sys
from pathlib import Path

pid = int(sys.argv[1])
expected_start = sys.argv[2]
try:
    fd = os.pidfd_open(pid)
except ProcessLookupError:
    sys.exit(0)
try:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        sys.exit(0)
    actual_start = stat.rsplit(")", 1)[1].split()[19]
    if actual_start != expected_start:
        sys.exit(0)
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    poller.poll()
finally:
    os.close(fd)
' "<PID>" "<開始時刻>"
```

開始前に対象が終了した場合や PID が再利用された場合も、元の実行は終了したものとして監視を終える。監視の起動失敗や権限エラーは、元の実行の終了とは扱わない

4. 監視の `run_dir` と元の実行識別子を対応付けて報告し、独立した作業を進めるかターンを終える。通知が届くまで元のツールをポーリングしない
5. 通知後は監視の `result.json` を確認する。`succeeded` は「終了待ちが完了した」という意味で、元のコマンドの成功を意味しない。監視が成功した場合だけ元のツールから出力・終了コードを一度回収する。Codex では控えた `session_id` に対して空の `chars` で `write_stdin` を呼ぶ。監視が失敗した場合は、原因を確認してから次を判断する

## 待っている間

`start` が成功したら、何をどこで待っているか（label・`watcher_pane`・`run_dir`）を1〜2行で報告して、ターンを終える

- コマンドの実行中、監視ペインは Herdr で `herdr-watch` という名前の作業中（`working`）のエージェントとして表示され、スペースも作業中になる。コマンドが終わると表示を外す。表示の報告に失敗しても監視は続け、`events.jsonl` に記録する
- 終了の通知が届くまで、監視ペインの読み取り、sleep、状態の確認をしない
- 待機と無関係にできる作業が残っていれば、先に進めてからターンを終えてよい。監視スクリプトは、呼び出し元のエージェントが入力待ち（`idle` / `done`）になるのを待ってから送る

## 通知を受けたら

`[herdr-watch:command <送り元>]` で始まるメッセージが届く

1. `<run_dir>/result.json` で `status`（`succeeded` / `failed` / `timed_out` / `interrupted` / `failed_to_start`）と `exit_code` を確認する
2. `<run_dir>/output.log` は、tail や検索で必要な部分だけ読む
3. `herdr pane close <watcher_pane>` で監視ペインを閉じる
4. 結果をもとに作業を続ける

## 委託する

委託先のエージェントを起動し、入力待ちになってから、タスクを `delegate` で送る

Codex は `herdr agent start --kind codex` ではなく、launcher の `codex-herdr` で起動する。`agent start` は素の `codex` を起動するので、委託先のコマンドに Herdr の環境変数が渡らず、`reply` が失敗する。Herdr は Codex を検出するまでエージェントとして扱わないため、入力欄が出るのを待ってから名前を付ける

```bash
herdr pane run <pane> codex-herdr
herdr pane wait-output <pane> --match "Ask Codex to do anything" --timeout 90000
herdr agent rename <pane> <name>
herdr agent wait <name> --until idle --until done --timeout 90000
```

Codex 以外は herdr skill の手順（`agent start`）で起動する

```bash
python3 "<このskillのディレクトリ>/scripts/herdr_watch.py" delegate --to <agent名かペインID> --label "<何を委託するか>" --prompt-file <タスクのファイル>
```

- 短いタスクは `--prompt "<タスク>"` で渡してよい
- `delegate` は、タスクの末尾に `reply` で返信する手順を付けて送り、委託先の完了を待たずに戻る。JSON で `run_dir`・`target_pane` を返す
- 委託先が入力待ち（`idle` / `done`）でない場合と、自分自身へ委託しようとした場合は送らずに失敗する
- 送ったら、何をどこへ委託したかを1〜2行で報告してターンを終える。委託先の画面の読み取りや `agent wait` をしない
- 追加の指示や質問への回答も `delegate` で送る。そのたびに新しい返信先ができる

委託先は、終わったら報告を Markdown で `<run_dir>/report.md` に書き、最後に次を実行してターンを終える。判断が必要で止まる場合も、質問を報告に書いて同じように返す。この手順は `delegate` がタスクの末尾に付ける

```bash
python3 "<このskillのディレクトリ>/scripts/herdr_watch.py" reply <run_dir> --report <run_dir>/report.md
```

`reply` は裏で通知用のプロセスを起こしてすぐ戻る。通知用のプロセスは、委託元が入力待ちになってから「<委託先> から「<label>」の返信が届いた。報告: <ファイル>」を送る。委託元は報告ファイルを読み、作業を続ける。委託先のペインを閉じるかどうかは、herdr skill の規則に従う

## メッセージの識別子

この skill が送るメッセージは、人間が書いたものではないことを示すため、先頭に `[herdr-watch:<種類> <送り元>]` を付ける。transcript の集計で、人間の発言と分ける手がかりにする

- 種類: `command`（コマンドの終了通知）、`delegate`（委託のタスク）、`reply`（委託先からの返信の通知）
- 送り元: 送った側のエージェントのセッション ID（Herdr の `agent_session.value`）の末尾8文字。`command` は監視を頼んだ自分自身。Codex のセッション ID は先頭が作成時刻なので、末尾を使う
- 送り元のセッション ID が Herdr から取れない場合は、何も送らずに失敗する

## 記録

`~/.local/state/herdr-watch/<run_id>/` に次を残す。削除しない

- `request.json`: 種類（`command` / `delegate`）、label、呼び出し元のペインとセッション。コマンドの場合はコマンド・作業ディレクトリ・監視ペイン、委託の場合は委託先
- `replies.jsonl`: 委託先からの返信（報告ファイル、送り元のペインとセッション、時刻）
- `notifier.log`: 返信を届ける裏のプロセスの出力
- `output.log`: コマンドの出力（標準出力と標準エラー）
- `result.json`: 状態、終了コード、開始・終了時刻、所要秒数
- `events.jsonl`: 監視・委託の開始、コマンドの終了、返信の受付、通知の成否

## 通知されない場合

次の場合は通知せず、`events.jsonl` に `notify_failed` と理由を記録する。コマンドの監視では監視ペインにも表示する

- 呼び出し元のペインのエージェントが、`start` や `delegate` を呼んだセッションから入れ替わっていた
- 呼び出し元のエージェントが承認・質問の画面（`blocked`）にいて、Herdr が送信を拒んだ

## 制約

- Codex の goal が active なセッションでは、ターンを終えるとすぐに goal の継続ターンが始まる。この skill を使っても待機を止められない
- 委託先が異常終了した、または承認・質問の画面で止まった場合、返信は届かない。委託先の状態は Herdr のサイドバーでユーザーが確認する
