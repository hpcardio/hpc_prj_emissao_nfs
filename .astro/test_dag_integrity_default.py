"""Teste de importacao das DAGs usado por ``astro dev parse``."""

from pathlib import Path

from airflow.models import DagBag


def test_dags_import_without_errors() -> None:
    project_root = Path(__file__).resolve().parents[1]
    exceptions_path = project_root / ".astro" / "dag_integrity_exceptions.txt"
    exceptions = {
        line.strip()
        for line in exceptions_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    dag_bag = DagBag(
        dag_folder=str(project_root / "dags"),
        include_examples=False,
    )
    errors = {
        str(Path(path).relative_to(project_root)): error
        for path, error in dag_bag.import_errors.items()
        if str(Path(path).relative_to(project_root)) not in exceptions
    }
    assert not errors, "\n".join(
        f"{path}: {error}" for path, error in sorted(errors.items())
    )
    expected_dags = {
        "emissao_nfse",
        "extracao_demonstrativo_conta_ipm",
        "extracao_nfse",
        "extracao_processos_virtuais_spu",
        "materializacao_glosas_ipm",
    }
    assert expected_dags.issubset(dag_bag.dag_ids), (
        f"DAGs ausentes: {sorted(expected_dags - set(dag_bag.dag_ids))}"
    )
