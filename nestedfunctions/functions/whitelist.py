import itertools
import logging
import os
from typing import List, Set

from pyspark.sql import DataFrame
from pyspark.sql.types import StructType

from nestedfunctions.functions.drop import drop
from nestedfunctions.processors.coreprocessor import CoreProcessor
from nestedfunctions.spark_schema.utility import SparkSchemaUtility
from nestedfunctions.utils.iterators.iterator_utils import distinct


def whitelist(df: DataFrame, fields: List[str]) -> DataFrame:
    return WhitelistProcessor(fields, ).process(df)


log = logging.getLogger(__name__)

# https://issues.apache.org/jira/browse/SPARK-28090
# Spark hangs when an execution plan has many projections on nested structs
NUMBER_OF_ITERATIONS_TO_FORCE_CATALYST_FLUSH = 10


class WhitelistProcessor(CoreProcessor):
    """Processor that selects only required fields.

      In case non-existing field provided -> this field is ignored::

      In case multiple fields with common root is provided -> parent field is selected

      """

    def __init__(self, whitelist_columns: List[str]):
        self.whitelist_columns = whitelist_columns
        self.schema_util = SparkSchemaUtility()

    def process(self, df: DataFrame) -> DataFrame:
        if self.no_fields_to_select(df):
            log.warning(
                f"No fields to select {self.whitelist_columns}. No intersections with {self.schema_util.flatten_schema(df.schema)}. "
                f"Returning empty df.")
            return df.limit(0)
        fields_to_drop = self.find_fields_to_drop(df)
        log.debug(f"Fields to drop {fields_to_drop}")
        return self.drop_fields(df, fields_to_drop)

    def find_fields_to_drop(self, df: DataFrame) -> Set[str]:
        only_existing_columns = self.__filter_only_existing_fields(df.schema, self.whitelist_columns)
        whitelist_parent_fields = set(filter_only_parents_fields(only_existing_columns))
        all_fields = SparkSchemaUtility().flatten_schema(df.schema)
        fields_to_drop = {field for field in all_fields if
                          self.is_field_need_to_be_dropped(field, whitelist_parent_fields)}
        return fields_to_drop

    @staticmethod
    def drop_fields(df: DataFrame, fields_to_drop: Set[str]) -> DataFrame:
        for idx, field_to_drop in enumerate(fields_to_drop):
            df = drop(df, field_to_drop)
            if (idx + 1) % NUMBER_OF_ITERATIONS_TO_FORCE_CATALYST_FLUSH == 0:
                log.debug("Dropping: time to force calculation")
                df = force_calculation(df)
        return df

    @staticmethod
    def is_field_need_to_be_dropped(field: str, parent_fields: Set[str]) -> bool:
        for parent_field in parent_fields:
            if parent_field in field:
                return False
        return True

    @staticmethod
    def __filter_only_existing_fields(schema: StructType, fields: List[str]) -> List[str]:
        schema_util = SparkSchemaUtility()
        res = []
        for field in distinct(fields):
            if schema_util.is_column_exist(schema, field):
                res.append(field)
            else:
                logging.warning(f"Field {field} is not found")
        return res

    def no_fields_to_select(self, df: DataFrame) -> bool:
        flattened_fields = set(self.schema_util.flatten_schema(df.schema))
        if (set(flattened_fields) - set(self.whitelist_columns)) == flattened_fields:
            return True
        else:
            return False


def filter_only_parents_fields(fields: List[str]) -> List[str]:
    combinations = itertools.combinations(fields, 2)
    common_elements = {os.path.commonprefix([c1, c2]) for (c1, c2) in combinations if
                       os.path.commonprefix([c1, c2]) != ""}
    root_elements = {f for f in fields if f in common_elements}
    if len(root_elements) != 0:
        logging.warning(f"Root elements found {root_elements}. Be careful!! Child elements will be ignored!")
    return __replace_elements_with_roots(fields, root_elements)


def __replace_elements_with_roots(fields: List[str], roots: Set[str]):
    res: List[str] = fields
    for root in roots:
        res = __replace_elements_with_root(res, root)
    return res


def __replace_elements_with_root(fields: List[str], root: str) -> List[str]:
    res = []
    root_added = False
    for f in fields:
        if root in f:
            if not root_added:
                root_added = True
                res.append(root)
        else:
            res.append(f)
    return res


def force_calculation(df: DataFrame) -> DataFrame:
    return df.cache()
