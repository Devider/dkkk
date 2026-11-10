"""Opt-in Cloud.ru batch/query compatibility probe; never run by pytest."""

from aigw_service.api.v2.embeddings import CloudEmbeddings, RetrievalSettings


def main() -> None:
    """Issue one document batch and one query only with configured credentials."""
    settings = RetrievalSettings()
    if not settings.api_key.get_secret_value():
        print("Skipped: set EMBEDDING_API_KEY to opt in to the Cloud.ru smoke request.")
        return
    client = CloudEmbeddings(settings)
    documents = client.embed_documents(["Цена меди", "Выручка"])
    query = client.embed_query("copper price")
    if len(query) != len(documents[0]):
        raise ValueError("Document/query dimensions differ")
    print(f"OK: {len(documents)} documents, dimension={len(query)}; raw strings, float encoding")


if __name__ == "__main__":
    main()
