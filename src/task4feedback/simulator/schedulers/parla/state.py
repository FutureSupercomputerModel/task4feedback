from ....types import *
from ...data import *
from ...device import *

from ..state import *
from ..architecture import *

from ....logging import logger

from dataclasses import dataclass, field, InitVar

StatesToResources: Dict[TaskState, list[ResourceType]] = {}
StatesToResources[TaskState.MAPPED] = [
    ResourceType.VCU,
    ResourceType.MEMORY,
    ResourceType.COPY,
]
StatesToResources[TaskState.LAUNCHED] = [ResourceType.VCU, ResourceType.COPY]
StatesToResources[TaskState.RESERVED] = [ResourceType.MEMORY]
StatesToResources[TaskState.COMPLETED] = []
AllResources = [ResourceType.VCU, ResourceType.MEMORY, ResourceType.COPY]


def get_required_memory_for_data(
    phase: TaskState, device: Device, data_id: DataID, objects: ObjectRegistry
) -> int:
    data = objects.get_data(data_id)
    if is_valid := data.is_valid(device, phase):
        return 0
    else:
        return data.size


def get_required_memory(
    memory: List[int],
    phase: TaskState,
    devices: Tuple[Device, ...],
    data_accesses: List[DataAccess],
    objects: ObjectRegistry,
) -> None:
    for data_access in data_accesses:
        idx = data_access.device
        device = devices[idx]
        memory[idx] += get_required_memory_for_data(
            phase, device, data_access.id, objects
        )


def get_required_resources(
    phase: TaskState,
    task: SimulatedTask,
    devices: Devices,
    objects: ObjectRegistry,
    count_data: bool = True,
    verbose: bool = False,
) -> List[ResourceSet]:
    if isinstance(devices, Device):
        devices = (devices,)

    task.set_resources(devices)

    memory: List[int] = [s[ResourceType.MEMORY] for s in task.resources]

    if count_data:
        get_required_memory(memory, phase, devices, task.read_accesses, objects)

    resources = []
    for i in range(len(devices)):
        t_req = task.resources[i]
        vcus: Numeric = t_req[ResourceType.VCU]
        mem: int = memory[i]
        copy: int = t_req[ResourceType.COPY]
        resources.append(ResourceSet(vcus=vcus, memory=mem, copy=copy))

    logger.resource.debug(
        "Required resources",
        extra=dict(task=task.name, phase=phase, resources=resources),
    )

    return resources


def _check_nearest_source(
    state: SystemState,
    task: SimulatedDataTask,
    verbose: bool = False,
) -> Optional[Device | SimulatedDevice]:
    assert isinstance(task, SimulatedDataTask)
    devices = task.assigned_devices
    assert devices is not None
    assert isinstance(devices, Device) or len(devices) == 1

    data_id = task.read_accesses[0].id
    data = state.objects.get_data(data_id)
    assert data is not None

    device_id = devices[0] if isinstance(devices, tuple) else devices
    device = state.objects.get_device(device_id)
    assert device is not None

    valid_sources_ids = data.get_devices_from_states(
        [TaskState.LAUNCHED], [DataState.VALID]
    )
    valid_sources = [state.objects.get_device(d) for d in valid_sources_ids]

    source_device = state.topology.nearest_valid_connection(
        device, valid_sources, require_copy_engines=True, require_symmetric=True
    )

    if logger.ENABLE_LOGGING:
        logger.data.debug(
            f"Finding nearest data source for {data.name} on {device_id}: {valid_sources_ids} -> {source_device}",
            extra=dict(
                data=data.name,
                target=device_id,
                valid_sources=valid_sources_ids,
                source=source_device,
                time=state.time,
            ),
        )

    return source_device


def _acquire_resources_mapped(
    state: SystemState, task: SimulatedTask, verbose: bool = False
):
    devices = task.assigned_devices

    if isinstance(task, SimulatedDataTask):
        raise RuntimeError(
            f"Data tasks should never hit the Mapper. Invalid task: {task}"
        )

    assert devices is not None
    if isinstance(devices, Device):
        devices = (devices,)

    resources = get_required_resources(
        TaskState.MAPPED, task, devices, state.objects, count_data=True
    )
    state.resource_pool.add_resources(
        devices, TaskState.MAPPED, AllResources, resources
    )

    if logger.ENABLE_LOGGING:
        for device in devices:
            remaining = state.resource_pool.pool[device][TaskState.MAPPED]

            logger.resource.debug(
                "Resources after acquiring",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining,
                    phase=TaskState.MAPPED,
                    pool=TaskState.MAPPED,
                    time=state.time,
                ),
            )


def _check_resources_reserved(
    state: SystemState, task: SimulatedTask, verbose: bool = False
) -> bool:
    devices = task.assigned_devices

    if isinstance(task, SimulatedDataTask):
        raise RuntimeError(
            f"Data tasks should never hit the Reserver. Invalid task: {task}"
        )

    assert devices is not None
    if isinstance(devices, Device):
        devices = (devices,)

    resources_types = StatesToResources[TaskState.RESERVED]

    resources = get_required_resources(
        TaskState.RESERVED, task, devices, state.objects, count_data=True
    )

    can_fit = state.resource_pool.check_resources(
        devices=devices,
        state=TaskState.RESERVED,
        types=resources_types,
        resources=resources,
    )

    return can_fit


def _acquire_resources_reserved(
    state: SystemState, task: SimulatedTask, verbose: bool = False
):
    devices = task.assigned_devices

    if isinstance(task, SimulatedDataTask):
        raise RuntimeError(
            f"Data tasks should never hit the Reserver. Invalid task: {task}"
        )

    assert devices is not None
    if isinstance(devices, Device):
        devices = (devices,)

    resource_types = StatesToResources[TaskState.RESERVED]

    resources = get_required_resources(
        TaskState.RESERVED, task, devices, state.objects, count_data=True
    )
    state.resource_pool.add_resources(
        devices, TaskState.RESERVED, resource_types, resources
    )

    if logger.ENABLE_LOGGING:
        for device in devices:
            remaining = state.resource_pool.pool[device][TaskState.RESERVED]
            logger.resource.debug(
                "Resources after acquiring",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining,
                    phase=TaskState.RESERVED,
                    pool=TaskState.RESERVED,
                    time=state.time,
                ),
            )


def _check_resources_launched(
    state: SystemState, task: SimulatedTask, verbose: bool = False
) -> bool:
    devices = task.assigned_devices

    assert devices is not None
    if isinstance(devices, Device):
        devices = (devices,)

    resources_types = StatesToResources[TaskState.LAUNCHED]

    resources = get_required_resources(
        TaskState.RESERVED, task, devices, state.objects, count_data=False
    )

    can_fit = state.resource_pool.check_resources(
        devices=devices,
        state=TaskState.RESERVED,
        types=resources_types,
        resources=resources,
    )

    if isinstance(task, SimulatedDataTask):
        source_device = _check_nearest_source(state, task)

        if source_device is None:
            return False

        if isinstance(source_device, SimulatedDevice):
            source_device = source_device.name

        task.source = source_device

    return can_fit


def _acquire_resources_launched(
    state: SystemState, task: SimulatedTask, verbose: bool = False
):
    devices = task.assigned_devices

    assert devices is not None
    if isinstance(devices, Device):
        devices = (devices,)

    resources = get_required_resources(
        TaskState.LAUNCHED, task, devices, state.objects, count_data=False
    )

    resource_types = StatesToResources[TaskState.LAUNCHED]

    state.resource_pool.add_resources(
        devices, TaskState.RESERVED, resource_types, resources
    )

    state.resource_pool.add_resources(
        devices, TaskState.LAUNCHED, AllResources, resources
    )

    if isinstance(task, SimulatedDataTask):
        assert len(devices) == 1
        target_device = devices[0]
        source_device = task.source
        assert source_device is not None

        state.topology.acquire_connection(source_device, target_device)
        if logger.ENABLE_LOGGING:
            logger.resource.info(
                "Acquired connection",
                extra=dict(
                    task=task.name,
                    source=source_device,
                    target=target_device,
                    time=state.time,
                ),
            )

    if logger.ENABLE_LOGGING:
        for device in devices:
            remaining_reserved = state.resource_pool.pool[device][TaskState.RESERVED]
            remaining_launched = state.resource_pool.pool[device][TaskState.LAUNCHED]

            logger.resource.debug(
                "Resources after acquiring",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining_reserved,
                    phase=TaskState.LAUNCHED,
                    pool=TaskState.RESERVED,
                    time=state.time,
                ),
            )

            logger.resource.debug(
                "Resources after acquiring",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining_launched,
                    phase=TaskState.LAUNCHED,
                    pool=TaskState.LAUNCHED,
                    time=state.time,
                ),
            )


def _release_resources_completed(
    state: SystemState, task: SimulatedTask, verbose: bool = False
):
    devices = task.assigned_devices

    assert devices is not None
    if isinstance(devices, Device):
        devices = (devices,)

    resources = get_required_resources(
        TaskState.LAUNCHED, task, devices, state.objects, count_data=False
    )

    # Free resources from all pools
    if isinstance(task, SimulatedComputeTask):
        state.resource_pool.remove_resources(
            devices=devices,
            state=TaskState.MAPPED,
            types=AllResources,
            resources=task.resources,
        )
    elif isinstance(task, SimulatedDataTask):
        assert len(devices) == 1
        target_device = devices[0]
        source_device = task.source
        assert source_device is not None

        state.topology.release_connection(source_device, target_device)
        if logger.ENABLE_LOGGING:
            logger.resource.info(
                "Released connection",
                extra=dict(
                    task=task.name,
                    source=source_device,
                    target=target_device,
                    time=state.time,
                ),
            )

    state.resource_pool.remove_resources(
        devices=devices,
        state=TaskState.RESERVED,
        types=AllResources,
        resources=task.resources,
    )

    state.resource_pool.remove_resources(
        devices=devices,
        state=TaskState.LAUNCHED,
        types=AllResources,
        resources=task.resources,
    )

    if logger.ENABLE_LOGGING:
        for device in devices:
            remaining_reserved = state.resource_pool.pool[device][TaskState.RESERVED]
            remaining_mapped = state.resource_pool.pool[device][TaskState.MAPPED]
            remaining_launched = state.resource_pool.pool[device][TaskState.LAUNCHED]

            logger.resource.debug(
                "Resources after releasing",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining_mapped,
                    phase=TaskState.COMPLETED,
                    pool=TaskState.MAPPED,
                    time=state.time,
                ),
            )

            logger.resource.debug(
                "Resources after releasing",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining_reserved,
                    phase=TaskState.COMPLETED,
                    pool=TaskState.RESERVED,
                    time=state.time,
                ),
            )

            logger.resource.debug(
                "Resources after releasing",
                extra=dict(
                    task=task.name,
                    device=device,
                    resources=remaining_launched,
                    phase=TaskState.COMPLETED,
                    pool=TaskState.LAUNCHED,
                ),
            )


def _use_data(
    state: SystemState,
    phase: TaskState,
    data_accesses: List[DataAccess],
    task: SimulatedTask,
    access_type: AccessType,
    verbose: bool = False,
):
    if len(data_accesses) == 0:
        return

    devices = task.assigned_devices
    assert devices is not None

    for data_access in data_accesses:
        data_id = data_access.id
        device_idx = data_access.device
        device = devices[device_idx]

        data = state.objects.get_data(data_id)
        assert data is not None

        update_state = True
        initial_state = False

        if phase == TaskState.MAPPED:
            initial_state = True
        elif phase == TaskState.RESERVED:
            data.finish_use(task.name, device, TaskState.MAPPED, operation=access_type)
        elif phase == TaskState.LAUNCHED:
            data.finish_use(
                task.name, device, TaskState.RESERVED, operation=access_type
            )
            # State updates at runtime are managed by data movement tasks
            # Compute tasks only verify and evict
            update_state = False

        data.start_use(
            task.name,
            device,
            phase,
            operation=access_type,
            update=update_state,
            verbose=verbose,
        )


def _release_data(
    state: SystemState,
    phase: TaskState,
    data_accesses: List[DataAccess],
    task: SimulatedTask,
    access_type: AccessType,
    verbose: bool = False,
):
    if len(data_accesses) == 0:
        return

    assert phase == TaskState.COMPLETED

    devices = task.assigned_devices
    assert devices is not None

    for data_access in data_accesses:
        data_id = data_access.id
        device_idx = data_access.device
        device = devices[device_idx]

        data = state.objects.get_data(data_id)
        assert data is not None

        data.finish_use(task.name, device, TaskState.LAUNCHED, operation=access_type)


def _move_data(
    state: SystemState,
    data_accesses: List[DataAccess],
    task: SimulatedTask,
    verbose: bool = False,
):
    if len(data_accesses) == 0:
        return

    devices = task.assigned_devices
    assert devices is not None

    # Move data is called from a data movement task at launch time
    assert isinstance(task, SimulatedDataTask)

    # move data is called from a data movement task at launch time
    # each data movement task moves one data item onto a single target device
    assert (
        len(data_accesses) == 1
    ), f"Data Task {task.name} should only move one data item: {data_accesses}"
    assert (
        len(devices) == 1
    ), f"Data Task {task.name} should only move to one device: {devices}"
    target_device = devices[0]
    assert target_device is not None

    data = state.objects.get_data(data_accesses[0].id)
    assert data is not None

    # Assumes source device is set by the prior check_resources_launched call
    source_device = task.source
    assert source_device is not None

    # Mark data as moving onto target device
    prior_state = data.start_move(task.name, source_device, target_device)


def _finish_move(
    state: SystemState, data_accesses: List[DataAccess], task: SimulatedTask
):
    if len(data_accesses) == 0:
        return

    devices = task.assigned_devices
    assert devices is not None

    # Move data is called from a data movement task at launch time
    assert isinstance(task, SimulatedDataTask)

    # move data is called from a data movement task at launch time
    # each data movement task moves one data item onto a single target device
    assert (
        len(data_accesses) == 1
    ), f"Data Task {task.name} should only move one data item: {data_accesses}"
    assert (
        len(devices) == 1
    ), f"Data Task {task.name} should only move to one device: {devices}"
    target_device = devices[0]
    assert target_device is not None

    data = state.objects.get_data(data_accesses[0].id)
    assert data is not None

    # Assumes source device is set by the prior check_resources_launched call
    source_device = task.source
    assert source_device is not None

    # Mark data as valid on target device
    prior_state = data.finish_move(task.name, source_device, target_device)


def _compute_task_duration(
    state: SystemState,
    task: SimulatedComputeTask,
    devices: Devices,
    verbose: bool = False,
) -> Tuple[Time, Time]:
    assert isinstance(task, SimulatedComputeTask)
    assert devices is not None

    runtime_infos = task.get_runtime_info(devices)
    max_time = max([runtime_info.task_time for runtime_info in runtime_infos])
    duration = Time(max_time)

    completion_time = state.time + duration
    return duration, completion_time


def _data_task_duration(
    state: SystemState,
    task: SimulatedDataTask,
    target_devices: Devices,
    verbose: bool = False,
) -> Tuple[Time, Time]:
    assert isinstance(task, SimulatedDataTask)
    assert target_devices is not None
    assert task.source is not None

    if isinstance(target_devices, Tuple):
        target = target_devices[0]
    else:
        target = target_devices

    data = state.objects.get_data(task.read_accesses[0].id)
    assert data is not None

    other_moving_tasks = data.get_tasks_from_usage(target, DataUses.MOVING_TO)
    if len(other_moving_tasks) > 0:
        duration = Time(0)
        other_task = other_moving_tasks[0]
        assert (
            other_task != task.name
        ), f"Current task {task} should not be in the list of moving tasks {other_moving_tasks} during duration calculation."

        other_task = state.objects.get_task(other_task)
        completion_time = other_task.completion_time
    else:
        duration = state.topology.get_transfer_time(task.source, target, data.size)
        completion_time = state.time + duration

    return duration, completion_time


@SchedulerOptions.register_state("parla")
@dataclass(slots=True)
class ParlaState(SystemState):
    def check_resources(
        self, phase: TaskState, task: SimulatedTask, verbose: bool = False
    ) -> bool:
        if phase == TaskState.MAPPED:
            return True
        elif phase == TaskState.RESERVED:
            return _check_resources_reserved(self, task)
        elif phase == TaskState.LAUNCHED:
            return _check_resources_launched(self, task)
        else:
            raise RuntimeError(
                f"Invalid phase {phase} in check_resource for task {task}"
            )

    def acquire_resources(
        self, phase: TaskState, task: SimulatedTask, verbose: bool = False
    ):
        if phase == TaskState.MAPPED:
            _acquire_resources_mapped(self, task)
        elif phase == TaskState.RESERVED:
            _acquire_resources_reserved(self, task)
        elif phase == TaskState.LAUNCHED:
            _acquire_resources_launched(self, task)
        else:
            raise RuntimeError(
                f"Invalid phase {phase} in acquire_resource for task {task}"
            )

    def release_resources(
        self, phase: TaskState, task: SimulatedTask, verbose: bool = False
    ):
        if phase == TaskState.COMPLETED:
            _release_resources_completed(self, task)
        else:
            raise RuntimeError(
                f"Invalid phase {phase} in release_resource for task {task}"
            )

    def use_data(self, phase: TaskState, task: SimulatedTask, verbose: bool = False):
        if isinstance(task, SimulatedComputeTask):
            _use_data(self, phase, task.read_accesses, task, AccessType.READ)
            _use_data(
                self, phase, task.read_write_accesses, task, AccessType.READ_WRITE
            )
            _use_data(self, phase, task.write_accesses, task, AccessType.WRITE)
        elif isinstance(task, SimulatedDataTask):
            # Data movement tasks only exist at launch time
            assert phase == TaskState.LAUNCHED

            # All data movement tasks are single data item tasks
            # They read from a single source device onto a single target device
            _move_data(self, task.read_accesses, task)
            _move_data(self, task.read_write_accesses, task)

    def release_data(
        self,
        phase: TaskState,
        task: SimulatedTask,
        verbose: bool = False,
    ):
        assert phase == TaskState.COMPLETED

        if isinstance(task, SimulatedComputeTask):
            _release_data(self, phase, task.read_accesses, task, AccessType.READ)
            _release_data(
                self, phase, task.read_write_accesses, task, AccessType.READ_WRITE
            )
            _release_data(self, phase, task.write_accesses, task, AccessType.WRITE)
        elif isinstance(task, SimulatedDataTask):
            _finish_move(self, task.read_accesses, task)
            _finish_move(self, task.read_write_accesses, task)

    def get_task_duration(
        self, task: SimulatedTask, devices: Devices, verbose: bool = False
    ) -> Tuple[Time, Time]:
        if isinstance(task, SimulatedComputeTask):
            return _compute_task_duration(self, task, devices, verbose=verbose)
        elif isinstance(task, SimulatedDataTask):
            return _data_task_duration(self, task, devices, verbose=verbose)
        else:
            raise RuntimeError(f"Invalid task type for {task} of type {type(task)}")

    def check_task_status(
        self, task: SimulatedTask, status: TaskStatus, verbose: bool = False
    ) -> bool:
        return task.check_status(status, self.objects.taskmap, self.time)
