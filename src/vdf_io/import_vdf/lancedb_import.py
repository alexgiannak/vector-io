from typing import Dict, List
from dotenv import load_dotenv
from tqdm import tqdm
import pyarrow.parquet as pq

import lancedb

from vdf_io.constants import INT_MAX
from vdf_io.meta_types import NamespaceMeta
from vdf_io.names import DBNames
from vdf_io.util import (
    set_arg_from_input,
    set_arg_from_password,
)
from vdf_io.import_vdf.vdf_import_cls import ImportVDB


load_dotenv()


class ImportLanceDB(ImportVDB):
    DB_NAME_SLUG = DBNames.LANCEDB

    @classmethod
    def import_vdb(cls, args):
        """
        Import data to LanceDB
        """
        set_arg_from_input(
            args,
            "endpoint",
            "Enter the URL of LanceDB instance (default: '~/.lancedb'): ",
            str,
            default_value="~/.lancedb",
        )
        set_arg_from_password(
            args,
            "lancedb_api_key",
            "Enter the LanceDB API key (default: value of os.environ['LANCEDB_API_KEY']): ",
            "LANCEDB_API_KEY",
        )
        lancedb_import = ImportLanceDB(args)
        lancedb_import.upsert_data()
        return lancedb_import

    @classmethod
    def make_parser(cls, subparsers):
        parser_lancedb = subparsers.add_parser(
            cls.DB_NAME_SLUG, help="Import data to lancedb"
        )
        parser_lancedb.add_argument(
            "--endpoint", type=str, help="Location of LanceDB instance"
        )
        parser_lancedb.add_argument(
            "--lancedb_api_key", type=str, help="LanceDB API key"
        )
        parser_lancedb.add_argument(
            "--tables", type=str, help="LanceDB tables to export (comma-separated)"
        )

    def __init__(self, args):
        # call super class constructor
        super().__init__(args)
        self.db = lancedb.connect(
            self.args["endpoint"], api_key=self.args.get("lancedb_api_key") or None
        )

    def upsert_data(self):
        max_hit = False
        self.total_imported_count = 0
        indexes_content: Dict[str, List[NamespaceMeta]] = self.vdf_meta["indexes"]
        index_names: List[str] = list(indexes_content.keys())
        if len(index_names) == 0:
            raise ValueError("No indexes found in VDF_META.json")
        tables = self.db.table_names()
        # Load Parquet file
        # print(indexes_content[index_names[0]]):List[NamespaceMeta]
        for index_name, index_meta in tqdm(
            indexes_content.items(), desc="Importing indexes"
        ):
            for namespace_meta in tqdm(index_meta, desc="Importing namespaces"):
                self.set_dims(namespace_meta, index_name)
                data_path = namespace_meta["data_path"]
                final_data_path = self.get_final_data_path(data_path)
                # Load the data from the parquet files
                parquet_files = self.get_parquet_files(final_data_path)

                vectors_all = {}
                for vec_col in namespace_meta.get("vector_columns", []):
                    vectors_all[vec_col] = {}

                new_index_name = index_name + (
                    f'_{namespace_meta["namespace"]}'
                    if namespace_meta["namespace"]
                    else ""
                )
                new_index_name = self.create_new_name(new_index_name, tables)
                vector_column_names, _ = self.get_vector_column_name(
                    new_index_name, namespace_meta, multi_vector_supported=True
                )
                if new_index_name not in tables:
                    table = self.db.create_table(
                        new_index_name, schema=pq.read_schema(parquet_files[0])
                    )
                else:
                    table = self.db.get_table(new_index_name)

                for file in tqdm(parquet_files, desc="Iterating parquet files"):
                    file_path = self.get_file_path(final_data_path, file)
                    df = self.read_parquet_progress(
                        file_path,
                        max_num_rows=(
                            (self.args.get("max_num_rows") or INT_MAX)
                            - self.total_imported_count
                        ),
                    )
                    # if there are additional columns in the parquet file, add them to the table
                    for col in df.columns:
                        if col not in table.columns:
                            table.add_column(col, df[col].dtype)
                    # split in batches
                    for batch in divide_into_batches(
                        df, self.args.get("batch_size", 1000)
                    ):
                        if self.total_imported_count + len(batch) >= (
                            self.args.get("max_num_rows") or INT_MAX
                        ):
                            batch = batch[
                                : (self.args.get("max_num_rows") or INT_MAX)
                                - self.total_imported_count
                            ]
                            max_hit = True
                        table.upsert(batch)
                        self.total_imported_count += len(batch)
                        if max_hit:
                            break
                if max_hit:
                    break
        print("Data imported successfully")


def divide_into_batches(df, batch_size):
    """
    Divide the dataframe into batches of size batch_size
    """
    for i in range(0, len(df), batch_size):
        yield df[i : i + batch_size]
