# Vendored LiteLLM Helm chart

Inference Stack uses a local copy of `litellm-helm` so it can bump PostgreSQL
without changing the rest of the LiteLLM chart. Build dependencies from
`charts/inference-stack` as usual. Helm packages the local chart and does not
need to download its Bitnami dependencies.

| Artifact | OCI manifest SHA-256 | Chart archive SHA-256 |
| --- | --- | --- |
| `oci://ghcr.io/berriai/litellm-helm:1.110.2` | `e6a3cf521e6bf82df3ff65c9f1ce1899dab161c18ae5dc9094fa9f8907b2d6b9` | `cae42bc0b4fc5c2ea4e6c4b8397979d4bd11fd1ccbb068cdc87cfa5e4e6c1bb9` |
| `oci://registry-1.docker.io/bitnamicharts/postgresql:18.12.27` | `32f21912266fef494348bb97e9ef229a0c791d397c0cbd410220e3f83a65be00` | `64c21d807b90de36a7428b2eae6254f48c9a5df6f101faaf566cbe20f5c4aa60` |
| `oci://registry-1.docker.io/bitnamicharts/redis:18.19.1` | `1e9f07f57b3825529247b3c3b63df60ba7d7c032d9512070ce8973df39b05ce3` | `b2fa1835f673a18002ca864c54fadac3c33789b26f6c5e58e2851b0b14a8f984` |

The local copy replaces the original nested charts with the published archives
above and updates the PostgreSQL dependency and compatibility values. Redis
has not changed. Docker Hub rate-limited the anonymous pull, so the Bitnami
archives came from `oci://mirror.gcr.io/bitnamicharts`. Their chart archive
digests match Artifact Hub, and the OCI manifest digests match Docker Hub.
