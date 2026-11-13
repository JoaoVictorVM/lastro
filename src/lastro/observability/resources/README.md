# Stack Langfuse local

`compose.yaml` é a stack usada por `lastro obs up`. Ela é copiada para a pasta privada do
projeto (`<dados do projeto>/observability/langfuse/`) junto com o arquivo `langfuse.env`, que
recebe segredos aleatórios gerados uma única vez e protegidos para o usuário atual.

## Proveniência

- Origem: `docker-compose.yml` do repositório oficial do Langfuse
  (<https://github.com/langfuse/langfuse>), licença MIT.
- Revisão de referência: commit `2365023d8542afc9b0d4e720a3bddc9ce301f342`
  (2026-11-12).
- Documentação de implantação: <https://langfuse.com/self-hosting/deployment/docker-compose>.

## Imagens fixadas

| Serviço | Imagem | Versão | Digest |
|---|---|---|---|
| langfuse-web | `docker.io/langfuse/langfuse` | 4.78.0 | `sha256:037188385ac6612d3494319fa060052da407b235502874c7ea79ade583c29002` |
| langfuse-worker | `docker.io/langfuse/langfuse-worker` | 4.78.0 | `sha256:175a32200b9908b283104e67b84d080381e841daa8ee6692f5bb26538df1a59e` |
| clickhouse | `docker.io/clickhouse/clickhouse-server` | 26.3.62.3 | `sha256:de3e47c64078aa196dae2349e048fc98ba81947f651f5c20e06e46baea8ae40a` |
| minio | `cgr.dev/chainguard/minio` | digest | `sha256:96b6cf3e14084d3427ec46de10c2873af61b945ad9272eec266cfc8df551cba5` |
| redis | `docker.io/library/redis` | 7.4.12 | `sha256:2e3eb11462c4e3759e6972678dd051d150ff54b1e60714b3c33b701260172a90` |
| postgres | `docker.io/library/postgres` | 17.12 | `sha256:dbb9623d5121b5d2c2c9e2fba56e40193cbb3510d4af7f035b1ab1823aff3205` |

## Diferenças em relação ao upstream

- Segredos obrigatórios (`${VAR:?}`), sem os valores padrão de exemplo do upstream.
- Somente a UI/OTLP do `langfuse-web` é publicada, em `127.0.0.1`; banco, ClickHouse, Redis e
  MinIO ficam na rede interna, sem portas no host. A mídia não é exportada pelo Lastro.
- Sem `langfuse-ai-gateway` nem variáveis de recursos de IA, que não participam da ingestão.
- `TELEMETRY_ENABLED=false`, rotação de logs `json-file` e `restart: unless-stopped`.

## Operação

- `lastro obs up` é idempotente: reutiliza segredos e volumes, nunca os rotaciona nem apaga.
- Para parar a stack: `docker compose -p <nome> -f compose.yaml --env-file langfuse.env stop`
  na pasta privada (o nome é exibido pelo comando). O Lastro não oferece comando destrutivo de
  volumes.
- Depois da primeira subida, crie usuário, organização, projeto e chaves pela UI e preencha o
  bloco `profiles.<nome>.observability` do `lastro.toml`.
