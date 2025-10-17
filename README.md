# Pivot AI（Kyosandai GCS Uploader）

https://aikyosandai-dev.streamlit.app

Pivot AI は、Dify Workflow と連携したチャット UI を Streamlit で提供するアプリです。  
左サイドバーからアップロードしたファイルを Google Cloud Storage（GCS）へ保存し、そのファイル名や各種オプションを Dify 渡しながら会話を進められます。

## デモ

https://github.com/user-attachments/assets/72a4ca30-6cef-435e-a29d-967bb7c9fe3f
 
- Dify Workflow: https://cloud.dify.ai/app/7fb6487e-dad6-4723-8b7e-7f559e984821/workflow
- GCS バケット: https://console.cloud.google.com/storage/browser/ai-pivot-chatagent-kyoto-sangyo-university/kyosandai
- リポジトリ: https://github.com/tyukei/ai_kyosandai

## 主な機能
<img width="1424" height="677" alt="image" src="https://github.com/user-attachments/assets/073cf833-36f2-4640-bb6c-d9658fd3863b" />

- GCS バケットへのファイルアップロードと公開設定（オプション）
- Dify Workflow とのストリーミング連携によるチャット UI
- 会話リセットや system prompt・RAG フラグなどのオプション指定
- secrets.toml によるシンプルな認証・設定管理

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

## アプリの使い方
- サイドバーの「ファイルアップロード」でファイルを選択すると即座に GCS にアップロードされ、ファイル名が Dify への入力として利用されます。
- `is_rag` や `system_prompt` を設定すると、その値が Dify Workflow の `inputs` に渡されます。
- チャット入力欄にメッセージを送信すると、Dify からのストリーミング応答が表示されます。
- 「会話をリセット」ボタンでセッション状態（会話履歴、ファイル ID、オプション）が初期化されます。

## デプロイ（Streamlit Community Cloud）
1. リポジトリを Streamlit Community Cloud にデプロイ対象として指定します。
2. Cloud 側の「Secrets」設定画面にローカルと同じ TOML 形式でシークレットを貼り付けます。
3. GCS バケットが外部アクセスを許可しているか、必要なら CORS 設定を確認してください。

## トラブルシューティング
- `GCS の設定が secrets.toml にありません`: `.streamlit/secrets.toml` が配置されているか、`[gcs]` セクションが正しく設定されているか確認します。
- `Uniform bucket-level access が有効なバケットでは ACL による公開設定が行えません`: `make_public = false` にするか、バケットのアクセス制御ポリシーを変更します。
- Dify から応答が返らない場合: API キーの権限、Workflow の状態、`base_url` の URL が正しいか確認します。

