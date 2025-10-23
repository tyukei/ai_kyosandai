# ピボットAI壁打ち君

https://aikyosandai.streamlit.app

ピボットAI壁打ち君 は、Dify Workflow と連携したチャット UI を Streamlit で提供するアプリです。  
左サイドバーからアップロードしたファイルを Google Cloud Storage（GCS）へ保存し、そのファイル名や各種オプションを Dify 渡しながら会話を進められます。

## デモ

https://github.com/user-attachments/assets/72a4ca30-6cef-435e-a29d-967bb7c9fe3f
 
- Dify Workflow: https://cloud.dify.ai/app/7fb6487e-dad6-4723-8b7e-7f559e984821/workflow
- GCS バケット: https://console.cloud.google.com/storage/browser/ai-pivot-chatagent-kyoto-sangyo-university/kyosandai
- リポジトリ: https://github.com/tyukei/ai_kyosandai


## アプリの使い方


- サイドバーの「ファイルアップロード」でファイルを選択すると即座に GCS にアップロードされ、ファイル名が Dify への入力として利用されます。
- `is_rag`（デフォルトで `true`）や `system_prompt` を設定すると、その値が Dify Workflow の `inputs` に渡されます。
- これまでの会話履歴は `history` 入力パラメータとして `user:質問\nassistant:回答` 形式で Dify に送信されます（最新のユーザー入力は `query` と重複しないよう除外）。
- チャット入力欄にメッセージを送信すると、Dify からのストリーミング応答が表示されます。
- 「会話をリセット」ボタンでセッション状態（会話履歴、ファイル ID、オプション）が初期化されます。
- サイドバー下部のバージョン表示は `APP_VERSION` 環境変数があればその値を、未設定の場合は `git describe --tags --always`（失敗時はコミット SHA）を表示します。



## システム構成

<img width="1424" height="677" alt="image" src="https://github.com/user-attachments/assets/073cf833-36f2-4640-bb6c-d9658fd3863b" />

- **開発環境（Streamlit + Python）**  
  ローカル開発者は Streamlit アプリと Python コードを編集します。完成したコードは GitHub リポジトリに push され、CI/CD のパイプラインに渡されます。
- **GitHub（コード管理 / CI・CD）**  
  リポジトリはアプリの単一ソースオブトゥルースです。Pull Request ベースで変更をレビューし、Streamlit Community Cloud などのホスト環境へデプロイを行います。
- **Streamlit ホスト環境**  
  デプロイ後のアプリは Streamlit Cloud 上で稼働し、利用者からの質問を受け付けます。ユーザー入力はサーバー内でセッション状態に保存され、Dify との通信やファイルアップロード処理を仲介します。
- **Dify（Gemini + RAG Workflow）**  
  Streamlit から送信されたクエリとオプション（ファイル ID、RAG フラグ、system prompt など）を受け取り、Gemini モデルと RAG データセットを用いた回答を生成します。回答はストリーミング形式で返却され、アプリ側でノイズ除去や整形を行ったうえで UI に反映されます。
- **Cloud Storage（GCS）**  
  利用者がアップロードしたファイルは GCS に保存されます。保存先のオブジェクト名がそのまま Dify への入力に使用され、RAG 参照やファイル提供に活用されます。
- **アプリ UI（Pivot AI）**  
  ブラウザ上のチャット画面で、ユーザーは質問を投げ、Dify からの回答をリアルタイムに受け取ります。サイドバーでは `is_rag` や `system_prompt` の切り替え、ファイルアップロード、会話リセット、バージョン情報の確認が可能です。

## 必要条件
- Python 3.10 以上を推奨
- Google Cloud Storage バケットとサービスアカウント（JSON キー）
- Dify の API キー（Workflow を呼び出す権限を持つもの）

## セットアップ

### 1. リポジトリの取得
```bash
git clone https://github.com/tyukei/ai_kyosandai_gcs.git
cd ai_kyosandai_gcs
```

### 2. 仮想環境と依存インストール
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Streamlit Secrets の設定
1. サンプルをコピーしてベースを作ります。
   ```bash
   cp .streamlit/secrets.example.toml .streamlit/secrets.toml
   ```
2. `.streamlit/secrets.toml` を編集し、以下の値を入力します。

#### `[gcs]` セクション
- `bucket_name`: アップロード先のバケット名。
- `project_id`: サービスアカウント JSON に含まれていれば省略可。
- `upload_prefix`: 任意。`prefix/` のように指定するとその配下に保存されます。
- `make_public`: `true` にするとアップロード直後に `blob.make_public()` を試行します。Uniform bucket-level access が有効な場合は `false` のままにしてください。

#### `[gcs.service_account]`
- Google Cloud で発行したサービスアカウント JSON を TOML 形式で貼り付けます。  
  そのまま JSON を貼る、または JSON 文字列を `service_account = """{...}"""` として指定することもできます。

#### `[dify]`
- `api_key`: Dify の API キー。
- `base_url`: 自前ホスティングしている場合のみ変更（既定は `https://api.dify.ai`）。
- `user`: 会話時のユーザー識別子（任意）。

### 4. ローカルで起動
```bash
streamlit run app.py
```
ブラウザが自動で開かない場合は http://localhost:8501/ にアクセスしてください。


## デプロイ（Streamlit Community Cloud）
1. リポジトリを Streamlit Community Cloud にデプロイ対象として指定します。
2. Cloud 側の「Secrets」設定画面にローカルと同じ TOML 形式でシークレットを貼り付けます。
3. GCS バケットが外部アクセスを許可しているか、必要なら CORS 設定を確認してください。
4. 任意で `APP_VERSION` 環境変数を設定すると、アプリ内のバージョン表示に反映されます（未設定でも Git メタデータで自動表示されます）。


## トラブルシューティング
- `GCS の設定が secrets.toml にありません`: `.streamlit/secrets.toml` が配置されているか、`[gcs]` セクションが正しく設定されているか確認します。
- `Uniform bucket-level access が有効なバケットでは ACL による公開設定が行えません`: `make_public = false` にするか、バケットのアクセス制御ポリシーを変更します。
- Dify から応答が返らない場合: API キーの権限、Workflow の状態、`base_url` の URL が正しいか確認します。
- アプリが起動されず以下の画面が表示される場合: 「Yes get this app back up !」を選択し、アプリを起動させてください。、一定期間たつとサーバが停止するので、起動する必要があります。
<img width="621" height="389" alt="image" src="https://github.com/user-attachments/assets/b2df8db5-0499-4daa-ba2d-1af758331ce6" />
