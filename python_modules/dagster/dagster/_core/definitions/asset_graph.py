from collections import defaultdict
from functools import cached_property
from typing import (
    AbstractSet,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Union,
)

from dagster import _check as check
from dagster._core.definitions.asset_check_spec import AssetCheckKey, AssetCheckSpec
from dagster._core.definitions.asset_spec import (
    SYSTEM_METADATA_KEY_AUTO_CREATED_STUB_ASSET,
    AssetExecutionType,
    AssetSpec,
)
from dagster._core.definitions.assets import AssetsDefinition
from dagster._core.definitions.auto_materialize_policy import AutoMaterializePolicy
from dagster._core.definitions.backfill_policy import BackfillPolicy
from dagster._core.definitions.base_asset_graph import (
    AssetKeyOrCheckKey,
    BaseAssetGraph,
    BaseAssetNode,
)
from dagster._core.definitions.dependency import (
    NodeHandle,
    NodeInputHandle,
    NodeOutput,
    NodeOutputHandle,
)
from dagster._core.definitions.events import AssetKey
from dagster._core.definitions.freshness_policy import FreshnessPolicy
from dagster._core.definitions.graph_definition import GraphDefinition
from dagster._core.definitions.metadata import ArbitraryMetadataMapping
from dagster._core.definitions.node_definition import NodeDefinition
from dagster._core.definitions.partition import PartitionsDefinition
from dagster._core.definitions.partition_mapping import PartitionMapping
from dagster._core.definitions.resolved_asset_deps import ResolvedAssetDependencies
from dagster._core.definitions.source_asset import SourceAsset
from dagster._core.definitions.utils import DEFAULT_GROUP_NAME
from dagster._core.selector.subset_selector import (
    generate_asset_dep_graph,
)


class AssetNode(BaseAssetNode):
    def __init__(
        self,
        key: AssetKey,
        parent_keys: AbstractSet[AssetKey],
        child_keys: AbstractSet[AssetKey],
        assets_def: AssetsDefinition,
        check_keys: AbstractSet[AssetCheckKey],
    ):
        self.key = key
        self.parent_keys = parent_keys
        self.child_keys = child_keys
        self.assets_def = assets_def
        self._check_keys = check_keys

    @property
    def description(self) -> Optional[str]:
        return self.assets_def.descriptions_by_key.get(self.key)

    @property
    def group_name(self) -> str:
        return self.assets_def.group_names_by_key.get(self.key, DEFAULT_GROUP_NAME)

    @property
    def is_materializable(self) -> bool:
        return self.assets_def.is_materializable

    @property
    def is_observable(self) -> bool:
        return self.assets_def.is_observable

    @property
    def is_external(self) -> bool:
        return self.assets_def.is_external

    @property
    def is_executable(self) -> bool:
        return self.assets_def.is_executable

    @property
    def metadata(self) -> ArbitraryMetadataMapping:
        return self.assets_def.specs_by_key[self.key].metadata or {}

    @property
    def tags(self) -> Mapping[str, str]:
        return self.assets_def.tags_by_key.get(self.key, {})

    @property
    def owners(self) -> Sequence[str]:
        return self.assets_def.specs_by_key[self.key].owners or []

    @property
    def is_partitioned(self) -> bool:
        return self.assets_def.partitions_def is not None

    @property
    def partitions_def(self) -> Optional[PartitionsDefinition]:
        return self.assets_def.partitions_def

    @property
    def partition_mappings(self) -> Mapping[AssetKey, PartitionMapping]:
        return self.assets_def.partition_mappings

    @property
    def freshness_policy(self) -> Optional[FreshnessPolicy]:
        return self.assets_def.freshness_policies_by_key.get(self.key)

    @property
    def auto_materialize_policy(self) -> Optional[AutoMaterializePolicy]:
        return self.assets_def.auto_materialize_policies_by_key.get(self.key)

    @property
    def auto_observe_interval_minutes(self) -> Optional[float]:
        return self.assets_def.auto_observe_interval_minutes

    @property
    def backfill_policy(self) -> Optional[BackfillPolicy]:
        return self.assets_def.backfill_policy

    @property
    def code_version(self) -> Optional[str]:
        return self.assets_def.specs_by_key[self.key].code_version

    @property
    def check_keys(self) -> AbstractSet[AssetCheckKey]:
        return self._check_keys

    @property
    def execution_set_asset_keys(self) -> AbstractSet[AssetKey]:
        return (
            {self.key}
            if len(self.assets_def.keys) <= 1 or self.assets_def.can_subset
            else self.assets_def.keys
        )

    @property
    def execution_set_asset_and_check_keys(self) -> AbstractSet[AssetKeyOrCheckKey]:
        if self.assets_def.can_subset:
            return {self.key}
        else:
            return self.assets_def.asset_and_check_keys

    ##### ASSET GRAPH SPECIFIC INTERFACE

    @property
    def execution_type(self) -> AssetExecutionType:
        return self.assets_def.execution_type

    @property
    def io_manager_key(self) -> str:
        return self.assets_def.get_io_manager_key_for_asset_key(self.key)


class AssetGraph(BaseAssetGraph[AssetNode]):
    _assets_defs_by_check_key: Mapping[AssetCheckKey, AssetsDefinition]
    _asset_keys_by_node_output_handle: Mapping[NodeOutputHandle, AssetKey]

    def __init__(
        self,
        asset_nodes_by_key: Mapping[AssetKey, AssetNode],
        assets_defs_by_check_key: Mapping[AssetCheckKey, AssetsDefinition],
        asset_keys_by_node_output_handle: Mapping[NodeOutputHandle, AssetKey],
    ):
        self._asset_nodes_by_key = asset_nodes_by_key
        self._assets_defs_by_check_key = assets_defs_by_check_key
        self._asset_keys_by_node_output_handle = asset_keys_by_node_output_handle

    @staticmethod
    def normalize_assets(
        assets: Iterable[Union[AssetsDefinition, SourceAsset]],
    ) -> Sequence[AssetsDefinition]:
        """Normalize a mixed list of AssetsDefinition and SourceAsset to a list of AssetsDefinition.

        Normalization includse:

        - Converting any SourceAsset to an AssetDefinition
        - Resolving all relative asset keys (that sometimes specify dependencies) to absolute asset
          keys
        - Creating unexecutable external asset definitions for any keys referenced by asset checks
          or as dependencies, but for which no definition was provided.
        """
        from dagster._core.definitions.external_asset import (
            create_external_asset_from_source_asset,
            external_asset_from_spec,
        )

        # Convert any source assets to external assets
        assets_defs = [
            create_external_asset_from_source_asset(a) if isinstance(a, SourceAsset) else a
            for a in assets
        ]
        all_keys = {k for asset_def in assets_defs for k in asset_def.keys}

        # Resolve all asset dependencies. An asset dependency is resolved when its key is an
        # AssetKey not subject to any further manipulation.
        resolved_deps = ResolvedAssetDependencies(assets_defs, [])
        assets_defs = [
            ad.with_attributes(
                input_asset_key_replacements={
                    raw_key: resolved_deps.get_resolved_asset_key_for_input(ad, input_name)
                    for input_name, raw_key in ad.keys_by_input_name.items()
                }
            )
            for ad in assets_defs
        ]

        # Create unexecutable external assets definitions for any referenced keys for which no
        # definition was provided.
        all_referenced_asset_keys = {
            key for assets_def in assets_defs for key in assets_def.dependency_keys
        }
        for key in all_referenced_asset_keys.difference(all_keys):
            assets_defs.append(
                external_asset_from_spec(
                    AssetSpec(key=key, metadata={SYSTEM_METADATA_KEY_AUTO_CREATED_STUB_ASSET: True})
                )
            )
        return assets_defs

    @classmethod
    def from_assets(
        cls,
        assets: Iterable[Union[AssetsDefinition, SourceAsset]],
    ) -> "AssetGraph":
        assets_defs = cls.normalize_assets(assets)

        # Build the set of AssetNodes. Each node holds key rather than object references to parent
        # and child nodes.
        dep_graph = generate_asset_dep_graph(assets_defs)

        assets_defs_by_check_key: Dict[AssetCheckKey, AssetsDefinition] = {}
        check_keys_by_asset_key: DefaultDict[AssetKey, Set[AssetCheckKey]] = defaultdict(set)
        for ad in assets_defs:
            for ck in ad.check_keys:
                check_keys_by_asset_key[ck.asset_key].add(ck)
                assets_defs_by_check_key[ck] = ad

        asset_nodes_by_key = {
            key: AssetNode(
                key=key,
                parent_keys=dep_graph["upstream"][key],
                child_keys=dep_graph["downstream"][key],
                assets_def=ad,
                check_keys=check_keys_by_asset_key[key],
            )
            for ad in assets_defs
            for key in ad.keys
        }

        assets_defs_by_op_node_names = cls._build_assets_defs_by_op_node_names(assets_defs)

        asset_key_by_input: Dict[NodeInputHandle, AssetKey] = {}
        asset_key_by_output: Dict[NodeOutputHandle, AssetKey] = {}
        check_key_by_output: Dict[NodeOutputHandle, AssetCheckKey] = {}

        # (
        #     dep_node_handles_by_asset_or_check_key,
        #     dep_node_output_handles_by_asset_or_check_key,
        # ) = asset_or_check_key_to_dep_node_handles(graph_def, assets_defs_by_outer_node_handle)

        # dep_node_handles_by_asset_key = {
        #     key: handles
        #     for key, handles in dep_node_handles_by_asset_or_check_key.items()
        #     if isinstance(key, AssetKey)
        # }
        # dep_node_output_handles_by_asset_key = {
        #     key: handles
        #     for key, handles in dep_node_output_handles_by_asset_or_check_key.items()
        #     if isinstance(key, AssetKey)
        # }

        node_output_handles_by_asset_check_key: Mapping[AssetCheckKey, NodeOutputHandle] = {}
        check_names_by_asset_key_by_node_handle: Dict[NodeHandle, Dict[AssetKey, Set[str]]] = {}
        assets_defs_by_check_key: Dict[AssetCheckKey, "AssetsDefinition"] = {}

        for op_node_name, assets_def in assets_defs_by_op_node_names.items():
            outer_node_handle = NodeHandle(op_node_name, None)
            for input_name, input_asset_key in assets_def.node_keys_by_input_name.items():
                outer_input_handle = NodeInputHandle(outer_node_handle, input_name)
                asset_key_by_input[outer_input_handle] = input_asset_key
                # resolve graph input to list of op inputs that consume it
                inner_input_handles = assets_def.node_def.resolve_input_to_destinations(
                    outer_input_handle
                )
                for inner_input_handles in inner_input_handles:
                    asset_key_by_input[inner_input_handles] = input_asset_key

            for output_name, asset_key in assets_def.node_keys_by_output_name.items():
                # resolve graph output to the op output it comes from
                inner_output_def, inner_node_handle = assets_def.node_def.resolve_output_to_origin(
                    output_name, handle=outer_node_handle
                )
                inner_output_handle = NodeOutputHandle(
                    check.not_none(inner_node_handle), inner_output_def.name
                )

                asset_key_by_output[inner_output_handle] = asset_key

                asset_key_by_input.update(
                    {
                        inner_input_handle: asset_key
                        for inner_input_handle in _resolve_output_to_destinations(
                            output_name, assets_def.node_def, outer_node_handle
                        )
                    }
                )

            if len(assets_def.check_specs_by_output_name) > 0:
                check_names_by_asset_key_by_node_handle[outer_node_handle] = defaultdict(set)

                for output_name, check_spec in assets_def.check_specs_by_output_name.items():
                    (
                        inner_output_def,
                        inner_node_handle,
                    ) = assets_def.node_def.resolve_output_to_origin(
                        output_name, handle=outer_node_handle
                    )
                    node_output_handle = NodeOutputHandle(
                        check.not_none(inner_node_handle), inner_output_def.name
                    )
                    node_output_handles_by_asset_check_key[check_spec.key] = node_output_handle
                    check_names_by_asset_key_by_node_handle[outer_node_handle][
                        check_spec.asset_key
                    ].add(check_spec.name)
                    check_key_by_output[node_output_handle] = check_spec.key

                assets_defs_by_check_key.update({k: assets_def for k in assets_def.check_keys})

        # dep_asset_keys_by_node_output_handle = defaultdict(set)
        # for asset_key, node_output_handles in dep_node_output_handles_by_asset_key.items():
        #     for node_output_handle in node_output_handles:
        #         dep_asset_keys_by_node_output_handle[node_output_handle].add(asset_key)

        # assets_defs_by_node_handle: Dict[NodeHandle, "AssetsDefinition"] = {
        #     # nodes for assets
        #     **{
        #         node_handle: asset_graph.get(asset_key).assets_def
        #         for asset_key, node_handles in dep_node_handles_by_asset_key.items()
        #         for node_handle in node_handles
        #     },
        #     # nodes for asset checks. Required for AssetsDefs that have selected checks
        #     # but not assets
        #     **{
        #         node_handle: assets_def
        #         for node_handle, assets_def in assets_defs_by_outer_node_handle.items()
        #         if assets_def.check_keys
        #     },
        # }

        # return AssetLayer(
        #     asset_keys_by_node_input_handle=asset_key_by_input,
        #     asset_info_by_node_output_handle=asset_key_by_output,
        #     check_key_by_node_output_handle=check_key_by_output,
        #     dependency_node_handles_by_asset_key=dep_node_handles_by_asset_key,
        #     dep_asset_keys_by_node_output_handle=dep_asset_keys_by_node_output_handle,
        #     node_output_handles_by_asset_check_key=node_output_handles_by_asset_check_key,
        #     check_names_by_asset_key_by_node_handle=check_names_by_asset_key_by_node_handle,
        # )

        return AssetGraph(
            asset_nodes_by_key=asset_nodes_by_key,
            assets_defs_by_check_key=assets_defs_by_check_key,
            asset_keys_by_node_output_handle=asset_key_by_output,
        )

    @staticmethod
    def _build_assets_defs_by_op_node_names(
        assets_defs: Sequence[AssetsDefinition],
    ) -> Mapping[str, AssetsDefinition]:
        # sort so that nodes get a consistent name
        assets_defs = sorted(assets_defs, key=lambda ad: (sorted((ak for ak in ad.keys))))

        # if the same graph/op is used in multiple assets_definitions, their invocations must have
        # different names. we keep track of definitions that share a name and add a suffix to their
        # invocations to solve this issue
        collisions: Dict[str, int] = {}
        result: Dict[str, AssetsDefinition] = {}
        for assets_def in (ad for ad in assets_defs if ad.is_executable):
            node_name = assets_def.node_def.name
            if collisions.get(node_name):
                collisions[node_name] += 1
                node_alias = f"{node_name}_{collisions[node_name]}"
            else:
                collisions[node_name] = 1
                node_alias = node_name

            # unique handle for each AssetsDefinition
            result[node_alias] = assets_def

        return result

    def get_for_node_output(self, node_output_handle: NodeOutputHandle) -> AssetNode:
        return self.get(self._asset_keys_by_node_output_handle[node_output_handle])

    def get_execution_set_asset_and_check_keys(
        self, asset_or_check_key: AssetKeyOrCheckKey
    ) -> AbstractSet[AssetKeyOrCheckKey]:
        if isinstance(asset_or_check_key, AssetKey):
            return self.get(asset_or_check_key).execution_set_asset_and_check_keys
        else:  # AssetCheckKey
            assets_def = self._assets_defs_by_check_key[asset_or_check_key]
            return (
                {asset_or_check_key} if assets_def.can_subset else assets_def.asset_and_check_keys
            )

    @cached_property
    def assets_defs(self) -> Sequence[AssetsDefinition]:
        return list(
            {
                *(asset.assets_def for asset in self.asset_nodes),
                *(ad for ad in self._assets_defs_by_check_key.values()),
            }
        )

    def assets_defs_for_keys(
        self, keys: Iterable[AssetKeyOrCheckKey]
    ) -> Sequence[AssetsDefinition]:
        return list(
            {
                *[self.get(key).assets_def for key in keys if isinstance(key, AssetKey)],
                *[
                    self._assets_defs_by_check_key[key]
                    for key in keys
                    if isinstance(key, AssetCheckKey)
                ],
            }
        )

    @cached_property
    def asset_check_keys(self) -> AbstractSet[AssetCheckKey]:
        return {key for ad in self.assets_defs for key in ad.check_keys}

    def get_spec_for_asset_check(self, asset_check_key: AssetCheckKey) -> Optional[AssetCheckSpec]:
        assets_def = self._assets_defs_by_check_key.get(asset_check_key)
        return assets_def.get_spec_for_check_key(asset_check_key) if assets_def else None


def materializable_in_same_run(
    asset_graph: BaseAssetGraph, child_key: AssetKey, parent_key: AssetKey
):
    """Returns whether a child asset can be materialized in the same run as a parent asset."""
    from dagster._core.definitions.partition_mapping import IdentityPartitionMapping
    from dagster._core.definitions.remote_asset_graph import RemoteAssetGraph
    from dagster._core.definitions.time_window_partition_mapping import TimeWindowPartitionMapping

    child_node = asset_graph.get(child_key)
    parent_node = asset_graph.get(parent_key)
    return (
        # both assets must be materializable
        child_node.is_materializable
        and parent_node.is_materializable
        # the parent must have the same partitioning
        and child_node.partitions_def == parent_node.partitions_def
        # the parent must have a simple partition mapping to the child
        and (
            not parent_node.is_partitioned
            or isinstance(
                asset_graph.get_partition_mapping(child_node.key, parent_node.key),
                (TimeWindowPartitionMapping, IdentityPartitionMapping),
            )
        )
        # the parent must be in the same repository to be materialized alongside the candidate
        and (
            not isinstance(asset_graph, RemoteAssetGraph)
            or asset_graph.get_repository_handle(child_key)
            == asset_graph.get_repository_handle(parent_key)
        )
    )


def _resolve_output_to_destinations(
    output_name: str, node_def: NodeDefinition, handle: NodeHandle
) -> Sequence[NodeInputHandle]:
    all_destinations: List[NodeInputHandle] = []
    if not isinstance(node_def, GraphDefinition):
        # must be in the op definition
        return all_destinations

    for mapping in node_def.output_mappings:
        if mapping.graph_output_name != output_name:
            continue
        output_pointer = mapping.maps_from
        output_node = node_def.node_named(output_pointer.node_name)

        all_destinations.extend(
            _resolve_output_to_destinations(
                output_pointer.output_name,
                output_node.definition,
                NodeHandle(output_pointer.node_name, parent=handle),
            )
        )

        output_def = output_node.definition.output_def_named(output_pointer.output_name)
        downstream_input_handles = (
            node_def.dependency_structure.output_to_downstream_inputs_for_node(
                output_pointer.node_name
            ).get(NodeOutput(output_node, output_def), [])
        )
        for input_handle in downstream_input_handles:
            all_destinations.append(
                NodeInputHandle(
                    NodeHandle(input_handle.node_name, parent=handle), input_handle.input_name
                )
            )

    return all_destinations
