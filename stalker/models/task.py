# -*- coding: utf-8 -*-
# Stalker a Production Asset Management System
# Copyright (C) 2009-2013 Erkan Ozgur Yilmaz
#
# This file is part of Stalker.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation;
# version 2.1 of the License.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

import datetime
import logging

from sqlalchemy import (Table, Column, Integer, ForeignKey, Boolean, Enum,
                        DateTime, Float, event)
from sqlalchemy.orm import relationship, validates, synonym

from stalker.db.session import DBSession
from stalker.db.declarative import Base
from stalker import defaults
from stalker.models import check_circular_dependency
from stalker.models.entity import Entity
from stalker.models.auth import User
from stalker.models.mixins import (DateRangeMixin, StatusMixin, ReferenceMixin,
                                   ScheduleMixin)
from stalker.exceptions import OverBookedError, CircularDependencyError
from stalker.log import logging_level

logger = logging.getLogger(__name__)
logger.setLevel(logging_level)


# schedule constraints
CONSTRAIN_NONE = 0
CONSTRAIN_START = 1
CONSTRAIN_END = 2
CONSTRAIN_BOTH = 3


class TimeLog(Entity, DateRangeMixin):
    """Holds information about the uninterrupted time spent on a specific
    :class:`.Task` by a specific :class:`.User`.

    It is so important to note that the TimeLog reports the **uninterrupted**
    time interval that is spent for a Task. Thus it doesn't care about the
    working time attributes like daily working hours, weekly working days or
    anything else. Again it is the uninterrupted time which is spent for a
    task.

    Entering a time log for 2 days will book the resource for 48 hours and not,
    2 * daily working hours.

    TimeLogs are created per resource. It means, you need to record all the
    works separately for each resource. So there is only one resource in a
    TimeLog instance.

    A :class:`.TimeLog` instance needs to be initialized with a :class:`.Task`
    and a :class:`.User` instances.

    Adding overlapping time log for a :class:`.User` will raise a
    :class:`.OverBookedError`.

    .. ::
      TimeLog instances automatically extends the :attr:`.Task.schedule_timing`
      of the assigned Task if the :attr:`.Task.total_logged_seconds` is getting
      bigger than the :attr:`.Task.schedule_timing` after this TimeLog.

    :param task: The :class:`.Task` instance that this time log belongs to.

    :param resource: The :class:`.User` instance that this time log is created
      for.
    """
    __auto_name__ = True
    __tablename__ = "TimeLogs"
    __mapper_args__ = {"polymorphic_identity": "TimeLog"}
    time_log_id = Column("id", Integer, ForeignKey("Entities.id"),
                         primary_key=True)
    task_id = Column(
        Integer, ForeignKey("Tasks.id"), nullable=False,
        doc="""The id of the related task."""
    )
    task = relationship(
        "Task",
        primaryjoin="TimeLogs.c.task_id==Tasks.c.id",
        uselist=False,
        back_populates="time_logs",
        doc="""The :class:`.Task` instance that this time log is created for"""
    )

    resource_id = Column(Integer, ForeignKey("Users.id"), nullable=False)
    resource = relationship(
        "User",
        primaryjoin="TimeLogs.c.resource_id==Users.c.id",
        uselist=False,
        back_populates="time_logs",
        doc="""The :class:`.User` instance that this time_log is created for"""
    )

    def __init__(
            self,
            task=None,
            resource=None,
            start=None,
            end=None,
            duration=None,
            **kwargs):
        super(TimeLog, self).__init__(**kwargs)
        kwargs['start'] = start
        kwargs['end'] = end
        kwargs['duration'] = duration
        DateRangeMixin.__init__(self, **kwargs)
        self.resource = resource
        self.task = task

    def _expand_task_schedule_timing(self, task):
        """Expands the task schedule timing if necessary

        :param task: The task that is going to be adjusted
        """
        # do the schedule_timing expansion here
        task_remaining_seconds = task.remaining_seconds
        tlog_total_seconds = self.duration.days * 86400 + self.duration.seconds

        logger.debug('task_remaining_seconds  : %s' % task_remaining_seconds)
        logger.debug('tlog_total_seconds      : %s' % tlog_total_seconds)

        if task_remaining_seconds < tlog_total_seconds:
            # expand the task
            # first convert the task.schedule_timing to seconds
            # then add the needed seconds to it then convert to least
            # meaningful time unit

            # do it normally
            task.schedule_timing, task.schedule_unit = \
                task.least_meaningful_time_unit(
                    task.schedule_seconds
                    + tlog_total_seconds
                    - task_remaining_seconds
                )

    @validates("task")
    def _validate_task(self, key, task):
        """validates the given task value
        """
        if not isinstance(task, Task):
            raise TypeError(
                "%s.task should be an instance of stalker.models.task.Task "
                "not %s" %
                (self.__class__.__name__, task.__class__.__name__)
            )

        # adjust task schedule
        with DBSession.no_autoflush:
            self._expand_task_schedule_timing(task)

        return task

    @validates("resource")
    def _validate_resource(self, key, resource):
        """validates the given resource value
        """

        if resource is None:
            raise TypeError(
                "%s.resource can not be None" % self.__class__.__name__
            )

        if not isinstance(resource, User):
            raise TypeError(
                "%s.resource should be a stalker.models.auth.User instance "
                "not %s" %
                (self.__class__.__name__, resource.__class__.__name__)
            )

        # check for overbooking
        logger.debug('resource.time_logs: %s' % resource.time_logs)
        for time_log in resource.time_logs:
            logger.debug('time_log       : %s' % time_log)
            logger.debug('time_log.start : %s' % time_log.start)
            logger.debug('time_log.end   : %s' % time_log.end)
            logger.debug('self.start     : %s' % self.start)
            logger.debug('self.end       : %s' % self.end)

            if time_log != self:
                if time_log.start == self.start or \
                   time_log.end == self.end or \
                   time_log.start < self.end < time_log.end or \
                   time_log.start < self.start < time_log.end:
                    raise OverBookedError(
                        "The resource %s is overly booked with %s and %s" %
                        (resource, self, time_log),
                    )
        return resource

    def __eq__(self, other):
        """equality of TimeLog instances
        """
        return isinstance(other, TimeLog) and self.task is other.task and \
            self.resource is other.resource and self.start == other.start and \
            self.end == other.end and self.name == other.name

# TODO: Consider contracting a Task with TimeLogs, what will happen when the task has logged in time
# TODO: Check, what happens when a task has TimeLogs and will have child task later on, will it be ok with TJ


def update_task_dates(func):
    """decorator that updates the date after the function finishes its job
    """
    def wrap(*args, **kwargs):
        # call the function as usual
        rvalue = func(*args, **kwargs)
        # update the dates
        task_class = args[0]
        args[0]._validate_dates(task_class.start, task_class.end, None)
        return rvalue

    # return decorated function
    return wrap


class Task(Entity, StatusMixin, DateRangeMixin, ReferenceMixin, ScheduleMixin):
    """Manages Task related data.

    **Introduction**

    Tasks are the smallest unit of work that should be accomplished to complete
    a :class:`.Project`. Tasks define a certain amount of time needed to be
    spent for a purpose. They also define a complex hierarchy of relation.

    Stalker follows and enhances the concepts stated in TaskJuggler_.

    .. _TaskJuggler : http://www.taskjuggler.org/

    .. note::
       .. versionadded:: 0.2.0
          References in Tasks

       Tasks can now have References.

    **Initialization**

    Tasks are a part of a bigger Project, that's way a Task needs to be created
    with a :class:`.Project` instance. It is possible to create a task without
    a project, if it is created to be a child of another task. And it is also
    possible to pass both a project and a parent task.

    But because passing both a project and a parent task may create an
    ambiguity, Stalker will raise a RuntimeWarning, if both project and task
    are given and the owner project of the given parent task is different then
    the supplied project instance. But again Stalker will warn the user but
    will continue to use the task as the parent and will correctly use the
    project of the given task as the project of the newly created task.

    The following codes are a couple of examples for creating Task instances::

      # with a project instance
      >>> task1 = Task(name='Schedule', project=proj1)

      # with a parent task
      >>> task2 = Task(name='Documentation', parent=task1)

      # or both
      >>> task3 = Task(name='Test', project=proj1, parent=task1)

      # this will create a RuntimeWarning
      >>> task4 = Task(name='Test', project=proj2, parent=task1)
      # task1 is not a # task of proj2

      >>> assert task4.project == proj1
      # Stalker uses the task1.project for task4

      # this will also create a RuntimeError
      >>> task3 = Task(name='Failure 2') # no project no parent, this is an
                                         # orphan task.

    Also initially Stalker will pin point the :attr:`.start` value and then
    will calculate proper :attr:`.end` and :attr:`.duration` values by using
    the :attr:`.schedule_timing` and :attr:`.schedule_unit` attributes. But
    these values (start, end and duration) are temporary values for an
    unscheduled task. The final date values will be calculated by TaskJuggler
    in the `auto scheduling` phase.

    **Auto Scheduling**

    Stalker uses TaskJuggler for task scheduling. After defining all the tasks,
    Stalker will convert them to a single tjp file along with the recorded
    :class:`.TimeLog`\ s :class:`.Vacation`\ s etc. and let TaskJuggler to
    solve the scheduling problem.

    During the auto scheduling (with TaskJuggler), the calculation of task
    duration, start and end dates are effected by the working hours setting of
    the :class:`.Studio`, the effort that needs to spend for that task and the
    availability of the resources assigned to the task.

    A good practice for creating a project plan is to supply the parent/child
    and dependency relation between tasks and the effort and resource
    information per task and leave the start and end date calculation to
    TaskJuggler.

    The default :attr:`.schedule_model` for Stalker tasks is 'effort`, the
    default :attr:`.schedule_unit` is ``hour`` and the default value of
    :attr:`.schedule_timing` is defined by the
    :attr:`stalker.config.Config.timing_resolution`. So for a config where the
    ``timing_resolution`` is set to 1 hour the schedule_timing is 1.

    It is also possible to use the ``length`` or ``duration`` values for
    :attr:`.schedule_model` (set it to 'effort', 'length' or 'duration' to get
    the desired scheduling model).

    To convert a Task instance to a TaskJuggler compatible string use the
    :attr:`.to_tjp`` attribute. It will try to create a good representation of
    the Task by using the resources, schedule_model, schedule_timing and
    schedule_constraint attributes.

    **Task to Task Relation**

    .. note::
       .. versionadded:: 0.2.0
       Task to Task Relation

    Tasks can have child Tasks. So you can create complex relations of Tasks to
    comply with your project needs.

    A Task is called a ``container task`` if it has at least one child Task.
    And it is called a ``leaf task`` if it doesn't have any children Tasks.
    Tasks which doesn't have a parent called ``root_task``.

    As opposed to TaskJuggler where the resource information is passed through
    parent to child, in Stalker the resources in a container task is
    meaningless, cause the resources are defined by the child tasks.

    .. note::

      Although, the ``tjp_task_template`` variable is not coded in that way in
      the default config, if you want to populate resource information through
      children tasks as it is in TaskJuggler, you can change the
      ``tjp_task_template`` variable with a local **config.py** file. See
      `configuring stalker`_

      .. _configuring stalker: ../configure.html

    Although the values are not very important after TaskJuggler schedules a
    task, the :attr:`~.start` and :attr:`~.end` values for a container
    task is gathered from the child tasks. The start is equal to the earliest
    start value of the children tasks, and the end is equal to the latest end
    value of the children tasks. Of course, these values are going to be
    ignored by TaskJuggler, but for interactive gantt charts these are good toy
    attributes to play with.

    Stalker will check if there will be a cycle if one wants to parent a Task
    to a child Task of its own or the dependency relation creates a cycle.

    In Gantt Charts the ``computed_start`` and ``computed_end`` attributes will
    be used if the task :attr:`.is_scheduled` is True.

    **Task Responsible**

    .. note::
       .. versionadded:: 0.2.0
          Task Responsible

    .. note::
       .. versionadded:: 0.2.5
          Multiple Responsible Per Task

    Tasks have a **responsible** which is a list of :class:`.User` instances
    who are responsible of the assigned task and all the hierarchy under it.

    If a task doesn't have any user assigned to its responsible attribute,
    then it will start to look to its parents until it can find a task with a
    responsible or there are no parent left, meaning the responsible is the
    :class:`.Project.lead` of the related :class:`.Project`.

    You can create complex responsibility chains for different branches of
    Tasks.

    **Percent Complete Calculation** .. versionadded:: 0.2.0

    Tasks can calculate how much it is completed based on the
    :attr:`.schedule_seconds` and :attr:`.total_logged_seconds` attributes.
    For a parent task, the calculation is based on the total
    :attr:`.schedule_seconds` and :attr:`.total_logged_seconds` attributes of
    their children. Even tough, the percent_complete attribute of a task is
    100% the task may not be considered as completed, because it may not be
    reviewed and approved by the responsible yet.

    **Task Review Workflow**

    .. versionadded:: 0.2.5
       Task Review Workflow

    Starting with Stalker v0.2.5 tasks are reviewed by their responsible users.
    The reviews done by responsible users will set the task status according to
    the supplied reviews. Please see the :class:`.Review` class documentation
    for more details.

    **Task Status Workflow**

    .. note::
       .. versionadded:: 0.2.5
          Task Status Workflow

    Task statuses now follow a workflow called "Task Status Workflow".

    The "Task Status Workflow" defines the different statuses that a Task will
    have along its normal life cycle. Container and leaf tasks will have
    different workflow using nearly the same set of statuses (container tasks
    have only 4 statuses where as leaf tasks have 9).

    The following diagram shows the status workflow for leaf tasks:

    .. image:: ../../../docs/source/images/Task_Status_Workflow.png
          :width: 637 px
          :height: 381 px
          :align: center

    The workflow defines the following statuses at described situations:

      +-----------------------------------------------------------------------+
      | LEAF TASK STATUS WORKFLOW                                             |
      +------------------+----------------------------------------------------+
      | Status Name      | Description                                        |
      +------------------+----------------------------------------------------+
      | Waiting For      | If a task has uncompleted dependencies then it     |
      | Dependency (WFD) | will have its status to set to WFD. A WFD Task can |
      |                  | not have a TimeLog or a review request can not be  |
      |                  | made for it.                                       |
      +------------------+----------------------------------------------------+
      | Ready To Start   | A task is set to RTS when there are no             |
      | (RTS)            | dependencies or all of its dependencies are        |
      |                  | completed, so there is nothing preventing it to be |
      |                  | started. An RTS Task can have new TimeLogs. A      |
      |                  | review can not be requested at this stage cause no |
      |                  | work is done yet.                                  |
      +------------------+----------------------------------------------------+
      | Work In Progress | A task is set to WIP when a TimeLog has been       |
      | (WIP)            | created for that task. A WIP task can have new     |
      |                  | TimeLogs and a review can be requested for that    |
      |                  | task.                                              |
      +------------------+----------------------------------------------------+
      | Pending Review   | A task is set to PREV when a new set of Review     |
      | (PREV)           | instances created for it by using the              |
      |                  | :meth:`.Task.request_review` method. And it is     |
      |                  | possible to request a review only for a task with  |
      |                  | status WIP. A PREV task can not have new TimeLogs  |
      |                  | nor a new request can be made because it is in     |
      |                  | already in review.                                 |
      +------------------+----------------------------------------------------+
      | Has Revision     | A task is set to HREV when one of its Reviews      |
      | (HREV)           | completed by requesting a review by using the      |
      |                  | :meth:`.Review.request_review` method. A HREV Task |
      |                  | can have new TimeLogs, and it will be converted to |
      |                  | a WIP or DREV depending to its dependency tasks.   |
      +------------------+----------------------------------------------------+
      | Dependency Has   | If a CMPL dependency task of a WIP, PREV, HREV,    |
      | Revision (DREV)  | DREV or CMPL task has a revision then depending on |
      |                  | to the reviewers choice the dependent tasks can    |
      |                  | have their status to be set to DREV which means    |
      |                  | both of the dependee and the dependent tasks can   |
      |                  | work at the same time. For a DREV task a review    |
      |                  | request can not be made until it is set to WIP     |
      |                  | again by setting the depending task to CMPL again. |
      +------------------+----------------------------------------------------+
      | On Hold (OH)     | A task is set to OH when the resource needs to     |
      |                  | work for another task, and the :meth:`Task.hold`   |
      |                  | is called. An OH Task can be resumed by calling    |
      |                  | :meth:`.Task.resume` method and depending to its   |
      |                  | :attr:`.Task.time_logs` attribute it will have its |
      |                  | status set to RTS or WIP.                          |
      +------------------+----------------------------------------------------+
      | Stopped (STOP)   | A task is set to STOP when no more work needs to   |
      |                  | done for that task and it will not be used         |
      |                  | anymore. Call :meth:`.Task.stop` method to do it   |
      |                  | properly. Only applicable to WIP tasks.            |
      |                  |                                                    |
      |                  | The schedule values of the task will be capped to  |
      |                  | current time spent on it, so Task Juggler will not |
      |                  | reserve any more resources for it.                 |
      +------------------+----------------------------------------------------+
      | Completed (CMPL) | A task is set to CMPL when all of the Reviews are  |
      |                  | completed by approving the task. It is not         |
      |                  | possible to create any new TimeLogs and no new     |
      |                  | review can be requested for a CMPL Task.           |
      +------------------+----------------------------------------------------+

    Container "Task Status Workflow" defines a set of statuses where the
    container task status will only change according to its children task
    statuses:

      +-----------------------------------------------------------------------+
      |                    CONTAINER TASK STATUS WORKFLOW                     |
      +------------------+----------------------------------------------------+
      | Status Name      | Description                                        |
      +------------------+----------------------------------------------------+
      | Waiting For      | If all of the child tasks are in WFD status then   |
      | Dependency (WFD) | the container task is also WFD.                    |
      +------------------+----------------------------------------------------+
      | Ready To Start   | A container task is set to RTS when children tasks |
      | (RTS)            | have statuses of only NEW and RTS.                 |
      +------------------+----------------------------------------------------+
      | Work In Progress | A container task is set to WIP when one of its     |
      | (WIP)            | children tasks have any of the statuses of RTS,    |
      |                  | WIP, PREV, HREV, DREV or CMPL.                     |
      +------------------+----------------------------------------------------+
      | Completed (CMPL) | A container task is set to CMPL when all of its    |
      |                  | children tasks are CMPL.                           |
      +------------------+----------------------------------------------------+

    **Arguments**

    :param project: A Task which doesn't have a parent (a root task) should be
      created with a :class:`.Project` instance. If it is skipped an no
      :attr:`.parent` is given then Stalker will raise a RuntimeError. If both
      the ``project`` and the :attr:`.parent` argument contains data and the
      project of the Task instance given with parent argument is different than
      the Project instance given with the ``project`` argument then a
      RuntimeWarning will be raised and the project of the parent task will be
      used.

    :type project: :class:`.Project`

    :param parent: The parent Task or Project of this Task. Every Task in
      Stalker should be related with a :class:`.Project` instance. So if no
      parent task is desired, at least a Project instance should be passed as
      the parent of the created Task or the Task will be an orphan task and
      Stalker will raise a RuntimeError.

    :type parent: :class:`Task`

    :param depends: A list of :class:`.Task`\ s that this :class:`.Task` is
      depending on. A Task can not depend to itself or any other Task which are
      already depending to this one in anyway or a CircularDependency error
      will be raised.

    :type depends: [:class:`Task`]

    :param resources: The :class:`.User`\ s assigned to this :class:`.Task`. A
      :class:`.Task` without any resource can not be scheduled.

    :type resources: [:class:`.User`]

    :param watchers: A list of :class:`.User` those are added this Task
      instance to their watchlist.

    :type watchers: [:class:`.User`]

    :param start: The start date and time of this task instance. It is only
      used if the :attr:`.schedule_constraint` attribute is set to
      :attr:`.CONSTRAIN_START` or :attr:`.CONSTRAIN_BOTH`. The default value
      is `datetime.datetime.now()`.

    :type start: :class:`datetime.datetime`

    :param end: The end date and time of this task instance. It is only used if
      the :attr:`.schedule_constraint` attribute is set to
      :attr:`.CONSTRAIN_END` or :attr:`.CONSTRAIN_BOTH`. The default value is
      `datetime.datetime.now()`.

    :type end: :class:`datetime.datetime`

    :param int schedule_timing: The value of the schedule timing.

    :param str schedule_unit: The unit value of the schedule timing. Should be
      one of 'min', 'h', 'd', 'w', 'm', 'y'.

    :param int schedule_constraint: The schedule constraint. It is the index
      of the schedule constraints value in
      :class:`stalker.config.Config.task_schedule_constraints`.

    :param int bid_timing: The initial bid for this Task. It can be used in
      measuring how accurate the initial guess was. It will be compared against
      the total amount of effort spend doing this task. Can be set to None,
      which will be set to the schedule_timing_day argument value if there is
      one or 0.

    :param str bid_unit: The unit of the bid value for this Task. Should be one
      of the 'min', 'h', 'd', 'w', 'm', 'y'.

    :param bool is_milestone: A bool (True or False) value showing if this task
      is a milestone which doesn't need any resource and effort.

    :param int priority: It is a number between 0 to 1000 which defines the
      priority of the :class:`.Task`. The higher the value the higher its
      priority. The default value is 500. Mainly used by TaskJuggler.
    """
    __auto_name__ = False
    __tablename__ = "Tasks"
    __mapper_args__ = {'polymorphic_identity': "Task"}
    task_id = Column(
        "id", Integer, ForeignKey('Entities.id'), primary_key=True,
        doc="""The ``primary_key`` attribute for the ``Tasks`` table used by
        SQLAlchemy to map this Task in relationships.
        """
    )

    project_id = Column(
        'project_id', Integer, ForeignKey('Projects.id'), nullable=True,
        doc="""The id of the owner :class:`.Project` of this Task. This
        attribute is mainly used by **SQLAlchemy** to map a :class:`.Project`
        instance to a Task.
        """
    )
    _project = relationship(
        'Project',
        primaryjoin='Tasks.c.project_id==Projects.c.id',
        back_populates='tasks',
        uselist=False,
        post_update=True,
    )

    parent_id = Column(
        'parent_id', Integer, ForeignKey('Tasks.id'),
        doc="""The id of the parent Task. Used by SQLAlchemy to map the
        :attr:`.parent` attr.
        """
    )
    parent = relationship(
        'Task',
        remote_side=[task_id],
        primaryjoin='Tasks.c.parent_id==Tasks.c.id',
        back_populates='children',
        post_update=True,
        doc="""A :class:`Task` instance which is the parent of this task.

        In Stalker it is possible to create a hierarchy of Tasks to comply
        with the need of the studio pipeline.
        """
    )

    children = relationship(
        'Task',
        primaryjoin='Tasks.c.parent_id==Tasks.c.id',
        back_populates='parent',
        post_update=True,
        cascade='all, delete-orphan',
        doc="""Other :class:`Task` instances which are the children of this
        Task instance. This attribute along with the :attr:`.parent` attribute
        is used in creating a DAG hierarchy of tasks.
        """
    )

    tasks = synonym(
        'children',
        doc="""A synonym for the :attr:`.children` attribute used by the
        descendants of the :class:`Task` class (currently :class:`.Asset`,
        :class:`.Shot` and :class:`.Sequence` classes).
        """
    )

    is_milestone = Column(
        Boolean,
        doc="""Specifies if this Task is a milestone.

        Milestones doesn't need any duration, any effort and any resources. It
        is used to create meaningful dependencies between the critical stages
        of the project.
        """
    )

    # TODO: is_complete should look to Task.status, but it is may be faster to
    #       query in this way, judge later
    is_complete = Column(
        Boolean,
        doc="""A bool value showing if this task is completed or not.

        There is a good article_ about why not to use an attribute called
        ``percent_complete`` to measure how much the task is completed.

        .. _article: http://www.pmhut.com/how-percent-complete-is-that-task-again
        """
    )

    depends = relationship(
        "Task",
        secondary="Task_Dependencies",
        primaryjoin="Tasks.c.id==Task_Dependencies.c.task_id",
        secondaryjoin="Task_Dependencies.c.depends_to_task_id==Tasks.c.id",
        backref="dependent_of",
        doc="""A list of :class:`.Task`\ s that this one is depending on.

        A CircularDependencyError will be raised when the task dependency
        creates a circular dependency which means it is not allowed to create
        a dependency for this Task which is depending on another one which in
        some way depends to this one again.
        """
    )

    resources = relationship(
        "User",
        secondary="Task_Resources",
        primaryjoin="Tasks.c.id==Task_Resources.c.task_id",
        secondaryjoin="Task_Resources.c.resource_id==Users.c.id",
        back_populates="tasks",
        doc="""The list of :class:`.User`\ s assigned to this Task.
        """
    )

    watchers = relationship(
        'User',
        secondary='Task_Watchers',
        primaryjoin='Tasks.c.id==Task_Watchers.c.task_id',
        secondaryjoin='Task_Watchers.c.watcher_id==Users.c.id',
        back_populates='watching',
        doc="""The list of :class:`.User`\ s watching this Task.
        """
    )

    _responsible = relationship(
        'User',
        secondary='Task_Responsible',
        primaryjoin='Tasks.c.id==Task_Responsible.c.task_id',
        secondaryjoin='Task_Responsible.c.responsible_id==Users.c.id',
        back_populates='responsible_of',
        doc="The list of :class:`.User`\ s responsible from this Task."
    )

    priority = Column(
        Integer,
        doc="""An integer number between 0 and 1000 used by TaskJuggler to
        determine the priority of this Task. The default value is 500.
        """
    )

    time_logs = relationship(
        "TimeLog",
        primaryjoin="TimeLogs.c.task_id==Tasks.c.id",
        back_populates="task",
        cascade='all, delete-orphan',
        doc="""A list of :class:`.TimeLog` instances showing who and when has
        spent how much effort on this task.
        """
    )

    versions = relationship(
        "Version",
        primaryjoin="Versions.c.task_id==Tasks.c.id",
        back_populates="task",
        cascade='all, delete-orphan',
        doc="""A list of :class:`.Version` instances showing the files created
        for this task.
        """
    )

    computed_start = Column(
        DateTime,
        doc="""A :class:`~datetime.datetime` instance showing the start value
        computed by **TaskJuggler**. It is None if this task is not scheduled
        yet.
        """
    )

    computed_end = Column(
        DateTime,
        doc="""A :class:`~datetime.datetime` instance showing the end value
        computed by **TaskJuggler**. It is None if this task is not scheduled
        yet.
        """
    )

    bid_timing = Column(
        Float, nullable=True, default=0,
        doc="""The value of the initial bid of this Task. It is an integer or
        a float.
        """
    )

    bid_unit = Column(
        Enum(*defaults.datetime_units, name='TaskBidUnit'),
        nullable=True,
        doc="""The unit of the initial bid of this Task. It is a string value.
        And should be one of 'min', 'h', 'd', 'w', 'm', 'y'.
        """
    )

    _schedule_seconds = Column(
        Integer, nullable=True,
        doc='cache column for schedule_seconds'
    )

    _total_logged_seconds = Column(
        Integer, nullable=True,
        doc='cache column for total_logged_seconds'
    )

    reviews = relationship(
        "Review",
        primaryjoin="Reviews.c.task_id==Tasks.c.id",
        back_populates="task",
        cascade='all, delete-orphan',
        doc="""A list of :class:`.Review` holding the details about the reviews
        created for this task."""
    )

    def __init__(self,
                 project=None,
                 parent=None,
                 depends=None,
                 resources=None,
                 responsible=None,
                 watchers=None,
                 start=None,
                 end=None,
                 schedule_timing=1.0,
                 schedule_unit='h',
                 schedule_model=None,
                 schedule_constraint=0,
                 bid_timing=None,
                 bid_unit=None,
                 is_milestone=False,
                 priority=defaults.task_priority,
                 **kwargs):

        # update kwargs with extras
        kwargs['start'] = start
        kwargs['end'] = end

        kwargs['schedule_timing'] = schedule_timing
        kwargs['schedule_unit'] = schedule_unit
        kwargs['schedule_model'] = schedule_model
        kwargs['schedule_constraint'] = schedule_constraint

        super(Task, self).__init__(**kwargs)

        # call the mixin __init__ methods
        StatusMixin.__init__(self, **kwargs)
        DateRangeMixin.__init__(self, **kwargs)
        ScheduleMixin.__init__(self, **kwargs)

        self.parent = parent
        self._project = project

        self.time_logs = []
        self.versions = []

        self.is_milestone = is_milestone
        self.is_complete = False

        if depends is None:
            depends = []

        self.depends = depends

        if self.is_milestone:
            resources = None

        if resources is None:
            resources = []
        self.resources = resources

        if watchers is None:
            watchers = []
        self.watchers = watchers

        if bid_timing is None:
            bid_timing = self.schedule_timing

        if bid_unit is None:
            bid_unit = self.schedule_unit

        self.bid_timing = bid_timing
        self.bid_unit = bid_unit
        self.priority = priority
        if responsible is None:
            responsible = []
        self.responsible = responsible

        # no matter what is given for the status
        # set the status to NEW for a newly created task
        with DBSession.no_autoflush:
            from stalker import Status
            status_new = Status.query.filter_by(code='NEW').first()
        self.status = status_new

    def __eq__(self, other):
        """the equality operator
        """
        return super(Task, self).__eq__(other) and isinstance(other, Task) \
            and self.project == other.project and self.parent == other.parent \
            and self.depends == other.depends and self.start == other.start \
            and self.end == other.end and self.resources == other.resources

    @validates("time_logs")
    def _validate_time_logs(self, key, time_log):
        """validates the given time_logs value
        """
        if not isinstance(time_log, TimeLog):
            raise TypeError(
                "%s.time_logs should be all stalker.models.task.TimeLog "
                "instances, not %s" % (
                    self.__class__.__name__, time_log.__class__.__name__
                )
            )

        # TODO: convert this to an event
        # update parents total_logged_second attribute
        with DBSession.no_autoflush:
            if self.parent:
                    self.parent.total_logged_seconds += time_log.total_seconds

        return time_log

    @validates("_reviews")
    def _validate_reviews(self, key, review):
        """validates the given review value
        """
        from stalker.models.review import Review
        if not isinstance(review, Review):
            raise TypeError(
                "%s.reviews should be all stalker.models.review.Review "
                "instances, not %s" % (
                    self.__class__.__name__,
                    review.__class__.__name__
                )
            )
        return review

    @validates("is_complete")
    def _validate_is_complete(self, key, complete_in):
        """validates the given complete value
        """
        return bool(complete_in)

    @validates("depends")
    def _validate_depends(self, key, depends):
        """validates the given depends value
        """
        if not isinstance(depends, Task):
            raise TypeError(
                "All the elements in the %s.depends should be an instance of "
                "stalker.models.task.Task, not %s" %
                (self.__class__.__name__, depends.__class__.__name__)
            )

        # check for the circular dependency
        check_circular_dependency(depends, self, 'depends')
        check_circular_dependency(depends, self, 'children')

        # check for circular dependency toward the parent, non of the parents
        # should be depending to the given depends_to_task
        with DBSession.no_autoflush:
            parent = self.parent
            while parent:
                if parent in depends.depends:
                    raise CircularDependencyError(
                        'One of the parents of %s is depending to %s' %
                        (self, depends)
                    )
                parent = parent.parent
        return depends

    @validates('schedule_timing')
    def _validate_schedule_timing(self, key, schedule_timing):
        """validates the given schedule_timing
        """
        schedule_timing = \
            ScheduleMixin._validate_schedule_timing(self, key, schedule_timing)

        # reschedule
        self._reschedule(schedule_timing, self.schedule_unit)

        return schedule_timing

    @validates('schedule_unit')
    def _validate_schedule_unit(self, key, schedule_unit):
        """validates the given schedule_unit
        """
        schedule_unit = \
            ScheduleMixin._validate_schedule_unit(self, key, schedule_unit)

        if self.schedule_timing:
            self._reschedule(self.schedule_timing, schedule_unit)

        return schedule_unit

    def _reschedule(self, schedule_timing, schedule_unit):
        """Updates the start and end date values by using the schedule_timing
        and schedule_unit values.

        :param schedule_timing: An integer or float value showing the value of
          the schedule timing.
        :type schedule_timing: int, float
        :param str schedule_unit: one of 'min', 'h', 'd', 'w', 'm', 'y'
        """
        # update end date value by using the start and calculated duration
        if self.is_leaf:
            unit = defaults.datetime_units_to_timedelta_kwargs.get(
                schedule_unit,
                None
            )
            if not unit:  # we are in a pre flushing state do not do anything
                return

            kwargs = {
                unit['name']: schedule_timing * unit['multiplier']
            }
            calculated_duration = datetime.timedelta(**kwargs)
            if self.schedule_constraint == CONSTRAIN_NONE or \
               self.schedule_constraint == CONSTRAIN_START:
                # get end
                self._start, self._end, self._duration = \
                    self._validate_dates(self.start, None, calculated_duration)
            elif self.schedule_constraint == CONSTRAIN_END:
                # get start
                self._start, self._end, self._duration = \
                    self._validate_dates(None, self.end, calculated_duration)
            elif self.schedule_constraint == CONSTRAIN_BOTH:
                # restore duration
                self._start, self._end, self._duration = \
                    self._validate_dates(self.start, self.end, None)

            # also update cached _schedule_seconds value
            self._schedule_seconds = self.schedule_seconds

    @validates("is_milestone")
    def _validate_is_milestone(self, key, is_milestone):
        """validates the given milestone value
        """
        if is_milestone:
            self.resources = []

        return bool(is_milestone)

    @validates('parent')
    def _validate_parent(self, key, parent):
        """validates the given parent value
        """
        if parent is not None:
            # if it is not a Task instance, go mad (cildir!!)
            if not isinstance(parent, Task):
                raise TypeError(
                    '%s.parent should be an instance of '
                    'stalker.models.task.Task, not %s' %
                    (self.__class__.__name__, parent.__class__.__name__)
                )

            # check for cycle
            check_circular_dependency(self, parent, 'children')
            check_circular_dependency(self, parent, 'depends')

        old_parent = self.parent
        new_parent = parent

        if old_parent:
            old_parent.schedule_seconds -= self.schedule_seconds
            old_parent.total_logged_seconds -= self.total_logged_seconds

        # update the new parent
        if new_parent:
            # if the new parent was a leaf task before this attachment
            # set schedule_seconds to 0
            if new_parent.is_leaf:
                new_parent.schedule_seconds = self.schedule_seconds
                new_parent.total_logged_seconds = self.total_logged_seconds
            else:
                new_parent.schedule_seconds += self.schedule_seconds
                new_parent.total_logged_seconds += self.total_logged_seconds

        return parent

    @validates('_project')
    def _validates_project(self, key, project):
        """validates the given project instance
        """
        if project is None:
            # check if there is a parent defined
            if self.parent:
                # use its project as the project

                # to prevent prematurely flush the parent
                with DBSession.no_autoflush:
                    project = self.parent.project

            else:
                # no project, no task, go mad again!!!
                raise TypeError(
                    '%s.project should be an instance of '
                    'stalker.models.project.Project, not %s. Or please supply '
                    'a stalker.models.task.Task with the parent argument, so '
                    'Stalker can use the project of the supplied parent task' %
                    (self.__class__.__name__, project.__class__.__name__)
                )

        from stalker import Project

        if not isinstance(project, Project):
            # go mad again it is not a project instance
            raise TypeError(
                '%s.project should be an instance of '
                'stalker.models.project.Project, not %s' %
                (self.__class__.__name__, project.__class__.__name__)
            )
        else:
            # check if there is a parent
            if self.parent:
                # check if given project is matching the parent.project
                with DBSession.no_autoflush:
                    if self.parent._project != project:
                        # don't go mad again, but warn the user that there is an
                        # ambiguity!!!
                        import warnings

                        message = 'The supplied parent and the project is not ' \
                                  'matching in %s, Stalker will use the parent ' \
                                  'project (%s) as the parent of this %s' % \
                                  (self,
                                   self.parent._project,
                                   self.__class__.__name__)

                        warnings.warn(message, RuntimeWarning)

                        # use the parent.project
                        project = self.parent._project
        return project

    @validates("priority")
    def _validate_priority(self, key, priority):
        """validates the given priority value
        """
        try:
            priority = int(priority)
        except (ValueError, TypeError):
            pass

        if not isinstance(priority, int):
            priority = defaults.task_priority

        if priority < 0:
            priority = 0
        elif priority > 1000:
            priority = 1000

        return priority

    @validates('children')
    def _validate_children(self, key, child):
        """validates the given child
        """
        # just empty the resources list
        # do it without a flush
        with DBSession.no_autoflush:
            self.resources = []

            # if this is the first ever child we receive
            # set total_scheduled_seconds to child's total_logged_seconds
            # and set schedule_seconds to child's schedule_seconds
            if self.is_leaf:
                # remove info from parent
                old_schedule_seconds = self.schedule_seconds
                self._total_logged_seconds = child.total_logged_seconds
                self._schedule_seconds = child.schedule_seconds
                # got a parent ?
                if self.parent:
                    # update schedule_seconds
                    self.parent._schedule_seconds -= old_schedule_seconds
                    self.parent._schedule_seconds += child.schedule_seconds

                # it was a leaf but now a parent, so set the start to max and
                # end to min
                self._start = datetime.datetime.max
                self._end = datetime.datetime.min

            # extend start and end dates
            self._expand_dates(self, child.start, child.end)

        return child

    @validates("resources")
    def _validate_resources(self, key, resource):
        """validates the given resources value
        """
        from stalker.models.auth import User

        if not isinstance(resource, User):
            raise TypeError(
                "%s.resources should be a list of "
                "stalker.models.auth.User instances, not %s" %
                (self.__class__.__name__, resource.__class__.__name__)
            )
        return resource

    @validates("watchers")
    def _validate_watchers(self, key, watcher):
        """validates the given watcher value
        """
        from stalker.models.auth import User

        if not isinstance(watcher, User):
            raise TypeError(
                "%s.watchers should be a list of "
                "stalker.models.auth.User instances not %s" %
                (self.__class__.__name__, watcher.__class__.__name__)
            )
        return watcher

    @validates("versions")
    def _validate_versions(self, key, version):
        """validates the given version value
        """
        from stalker.models.version import Version

        if not isinstance(version, Version):
            raise TypeError(
                "%(class)s.versions should only have"
                "stalker.models.version.Version instances, and not %(class)s" %
                {'class': self.__class__.__name__}
            )

        return version

    @validates('bid_timing')
    def _validate_bid_timing(self, key, bid_timing):
        """validates the given bid_timing value
        """
        if bid_timing is not None:
            if not isinstance(bid_timing, (int, float)):
                raise TypeError(
                    '%(class)s.bid_timing should be an integer or float '
                    'showing the value of the initial bid for this %(class)s, '
                    'not %(bid)s' %
                    {
                        'class': self.__class__.__name__,
                        'bid': bid_timing.__class__.__name__
                    }
                )
        return bid_timing

    @validates('bid_unit')
    def _validate_bid_unit(self, key, bid_unit):
        """validates the given bid_unit value
        """
        if bid_unit is None:
            bid_unit = 'h'

        if not isinstance(bid_unit, (str, unicode)):
            raise TypeError(
                '%(class)s.bid_unit should be a string value one of %(units)s '
                'showing the unit of the bid timing of this %(class)s, not '
                '%(bid)s' % {
                    'class': self.__class__.__name__,
                    'units': defaults.datetime_units,
                    'bid': bid_unit.__class__.__name__
                }
            )

        if bid_unit not in defaults.datetime_units:
            raise ValueError(
                '%(class)s.bid_unit should be a string value one of %(units)s '
                'showing the unit of the bid timing of this %(class)s, not '
                '%(bid)s' % {
                    'class': self.__class__.__name__,
                    'units': defaults.datetime_units,
                    'bid': bid_unit.__class__.__name__
                }
            )

        return bid_unit

    def _expand_dates(self, task, start, end):
        """extends the given tasks date values with the given start and end
        values
        """
        # update parents start and end date
        if task:
            if task.start > start:
                task.start = start
                #logger.debug('start is updated to : %s' % start)
            if task.end < end:
                task.end = end
                #logger.debug('end is updated to   : %s' % end)

    @validates('computed_start')
    def _validate_computed_start(self, key, computed_start):
        """validates the given computed_start value
        """
        self.start = computed_start
        return computed_start

    @validates('computed_end')
    def _validate_computed_end(self, key, computed_end):
        """validates the given computed_end value
        """
        self.end = computed_end
        return computed_end

    def _validate_start(self, start_in):
        """validates the given start value
        """
        if start_in is None:
            start_in = self.project.round_time(datetime.datetime.now())
        elif not isinstance(start_in, datetime.datetime):
            raise TypeError(
                '%s.start should be an instance of datetime.datetime, not %s' %
                (self.__class__.__name__, start_in.__class__.__name__)
            )
        return start_in

    def _start_getter(self):
        """overridden start getter
        """
        return self._start

    def _start_setter(self, start_in):
        """overridden start setter
        """
        self._start, self._end, self._duration = \
            self._validate_dates(start_in, self._end, self._duration)
        self._expand_dates(self.parent, self.start, self.end)

    def _end_getter(self):
        """overridden end getter
        """
        return self._end

    def _end_setter(self, end_in):
        """overridden end setter
        """
        # update the end only if this is not a container task
        self._start, self._end, self._duration = \
            self._validate_dates(self.start, end_in, self.duration)
        self._expand_dates(self.parent, self.start, self.end)

    def _project_getter(self):
        return self._project

    project = synonym(
        '_project',
        descriptor=property(
            _project_getter
        ),
        doc="""The owner Project of this task.

        It is a read-only attribute. It is not possible to change the owner
        Project of a Task it is defined when the Task is created.
        """
    )

    @property
    def is_root(self):
        """Returns True if the Task has no parent
        """
        return not bool(self.parent)

    @property
    def is_container(self):
        """Returns True if the Task has children Tasks
        """
        with DBSession.no_autoflush:
            return bool(len(self.children))

    @property
    def is_leaf(self):
        """Returns True if the Task has no children Tasks
        """
        return not self.is_container

    @property
    def parents(self):
        """Returns all of the parents of this Task starting from the root
        """
        parents = []
        task = self.parent
        while task:
            parents.append(task)
            task = task.parent
        parents.reverse()
        return parents

    @property
    def tjp_abs_id(self):
        """returns the calculated absolute id of this task
        """
        if self.parent:
            abs_id = self.parent.tjp_abs_id
        else:
            abs_id = self.project.tjp_id

        return '%s.%s' % (abs_id, self.tjp_id)

    @property
    def to_tjp(self):
        """TaskJuggler representation of this task
        """
        from jinja2 import Template

        temp = Template(defaults.tjp_task_template)
        return temp.render({'task': self})

    @property
    def level(self):
        """Returns the level of this task. It is a temporary property and will
        be useless when Stalker has its own implementation of a proper Gantt
        Chart. Write now it is used by the jQueryGantt.
        """
        i = 0
        current = self
        while current:
            i += 1
            current = current.parent
        return i

    @property
    def is_scheduled(self):
        """A predicate which returns True if this task has both a
        computed_start and computed_end values
        """
        return self.computed_start is not None and \
            self.computed_end is not None

    def _total_logged_seconds_getter(self):
        """The total effort spent for this Task. It is the sum of all the
        TimeLogs recorded for this task as seconds.

        :returns int: An integer showing the total seconds spent.
        """
        seconds = 0
        with DBSession.no_autoflush:
            if self.is_leaf:
                for time_log in self.time_logs:
                    seconds += time_log.total_seconds
            else:
                if self._total_logged_seconds is None:
                    self.update_schedule_info()
                return self._total_logged_seconds
        return seconds

    def _total_logged_seconds_setter(self, seconds):
        """Setter for total_logged_seconds. Mainly used for container tasks, to
        cache the child logged_seconds

        :param seconds: An integer value for the seconds
        """
        # only set for container tasks
        if self.is_container:
            # update parent
            old_value = 0
            if self._total_logged_seconds:
                old_value = self._total_logged_seconds
            self._total_logged_seconds = seconds
            if self.parent:
                self.parent.total_logged_seconds = \
                    self.parent.total_logged_seconds - old_value + seconds

    total_logged_seconds = synonym(
        '_total_logged_seconds',
        descriptor=property(
            _total_logged_seconds_getter,
            _total_logged_seconds_setter
        )
    )

    def _schedule_seconds_getter(self):
        """returns the total effort, length or duration in seconds, for
        completeness calculation
        """
        # for container tasks use the children schedule_seconds attribute
        if self.is_container:
            if self._schedule_seconds is None or self._schedule_seconds < 0:
                self.update_schedule_info()
            return self._schedule_seconds
        else:
            return self.to_seconds(
                self.schedule_timing,
                self.schedule_unit,
                self.schedule_model
            )

    def _schedule_seconds_setter(self, seconds):
        """Sets the schedule_seconds of this task. Mainly used for container
        tasks.

        :param seconds: An integer value of schedule_seconds for this task.
        :return:
        """
        # do it only for container tasks
        if self.is_container:
            # also update the parents
            with DBSession.no_autoflush:
                if self.parent:
                    current_value = 0
                    if self._schedule_seconds:
                        current_value = self._schedule_seconds
                    self.parent.schedule_seconds = \
                        self.parent.schedule_seconds - current_value + seconds
                self._schedule_seconds = seconds

    schedule_seconds = synonym(
        '_schedule_seconds',
        descriptor=property(
            _schedule_seconds_getter,
            _schedule_seconds_setter
        )
    )

    def update_schedule_info(self):
        """updates the total_logged_seconds and schedule_seconds attributes by
        using the children info and triggers an update on every children
        """
        if self.is_container:
            total_logged_seconds = 0
            schedule_seconds = 0
            logger.debug('updating schedule info for : %s' % self.name)
            for child in self.children:
                # update children if they are a container task
                if child.is_container:
                    child.update_schedule_info()
                if child.schedule_seconds:
                    schedule_seconds += child.schedule_seconds
                if child.total_logged_seconds:
                    total_logged_seconds += child.total_logged_seconds

            self._schedule_seconds = schedule_seconds
            self._total_logged_seconds = total_logged_seconds
        else:
            self._schedule_seconds = self.schedule_seconds
            self._total_logged_seconds = self.total_logged_seconds

    @property
    def percent_complete(self):
        """returns the percent_complete based on the total_logged_seconds and
        schedule_seconds of the task. Container tasks will use info from their
        children
        """
        if self.is_container:
            if self.total_logged_seconds is None or \
               self.schedule_seconds is None:
                self.update_schedule_info()
        return self.total_logged_seconds / float(self.schedule_seconds) * 100

    @property
    def remaining_seconds(self):
        """returns the remaining amount of efforts, length or duration left
        in this Task as seconds.
        """
        # for effort based tasks use the time_logs
        return self.schedule_seconds - self.total_logged_seconds

    def _responsible_getter(self):
        """returns the current responsible of this task
        """
        if self._responsible:
            return self._responsible
        else:
            # traverse parents
            for parent in reversed(self.parents):
                if parent.responsible:
                    return parent.responsible

        # use the project lead
        if self.project.lead:
            return [self.project.lead]
        else:
            # no project lead, strange, return an empty list
            return []

    def _responsible_setter(self, responsible):
        """sets the responsible attribute

        :param responsible: A :class:`.User` instance
        """
        self._responsible = responsible

    @validates('_responsible')
    def _validate_responsible(self, key, responsible):
        """validates the given responsible value
        """
        from stalker.models.auth import User
        if not isinstance(responsible, User):
            raise TypeError(
                '%s.responsible should be an instance of '
                'stalker.models.auth.User, not %s' %
                (self.__class__.__name__, responsible.__class__.__name__)
            )
        return responsible

    responsible = synonym(
        '_responsible',
        descriptor=property(
            _responsible_getter,
            _responsible_setter,
            doc="""The responsible of this task.

            This attribute will return the responsible of this task which is a
            :class:`.User` instance. If there is no
            responsible set for this task, then it will try to find a
            responsible in its parents and will return the project.lead if it
            can not find anybody.
            """
        )
    )

    @property
    def tickets(self):
        """returns the tickets referencing this task in their links attribute
        """
        from stalker import Ticket
        return Ticket.query.filter(Ticket.links.contains(self)).all()

    @property
    def open_tickets(self):
        """returns the open tickets referencing this task in their links
        attribute
        """
        from stalker import Ticket, Status
        status_closed = Status.query.filter(Status.name == 'Closed').first()
        return Ticket.query\
            .filter(Ticket.links.contains(self))\
            .filter(Ticket.status != status_closed).all()

    # =============
    # ** ACTIONS **
    # =============
    def create_time_log(self, resource, start, end):
        """A helper method to create TimeLogs, this will ease creating TimeLog
        instances for task.
        """
        raise NotImplementedError

    def request_review(self):
        """Creates and returns Review instances for each of the responsible of
        this task and sets the task status to PREV. Also calling this method
        will cap the schedule timing of this task to current time spent on it.
        Thus a task with 4 days of effort can be capped to 2 days if a review
        is requested at day 2. Only applicable to leaf tasks.
        """
        raise NotImplementedError

    def request_revision(self, reviews):
        """Requests revision.

        Only applicable for PREV leaf tasks. This method will expand the
        schedule timings of the task by looking at the supplied reviews. Only
        RREV Reviews are considered, and if non of the supplied
        :class:`.Review` instances are reviews with RREV status then the task
        will be set to CMPL.

        :param reviews: A list of Review instances, where they have APP or RREV
          statuses.

        :type reviews: list of class:`.Review` instances.
        """
        raise NotImplementedError

    def hold(self):
        """Pauses the execution of this task by setting its status to OH. Only
        applicable to RTS and WIP tasks, any task with other statuses will
        raise a ValueError.
        """
        raise NotImplementedError

    def stop(self):
        """Stops this task. It is nearly equivalent to deleting this task. But
        this will at least preserve the TimeLogs entered for this task. Only
        applicable to tasks with status WIP. You can use :meth:`.resume` to
        resume the task. The only difference between :meth:`.hold` (other than
        setting the task to different statuses) is the schedule info, while the
        hold() method will preserve the schedule info, stop will set the
        schedule info to the current effort. So if 2 days of effort has been
        entered for a 4 days task, when stopped the task effort will be capped
        to 2 days, thus TaskJuggler will not try to reserve any resource for
        this task anymore.
        """
        raise NotImplementedError

    def resume(self):
        """Resumes the execution of this task by setting its status to RTS or
        WIP depending to its time_logs attribute, so if it has TimeLogs then it
        will resume as WIP and if it doesn't then it will resume as RTS. Only
        applicable to Tasks with status OH.
        """
        raise NotImplementedError

    def approve(self):
        """Approves the task and sets its status to CMPL.
        """
        raise NotImplementedError


# TASK_DEPENDENCIES
Task_Dependencies = Table(
    "Task_Dependencies", Base.metadata,
    Column("task_id", Integer, ForeignKey("Tasks.id"), primary_key=True),
    Column("depends_to_task_id", Integer, ForeignKey("Tasks.id"),
           primary_key=True)
)

# TASK_RESOURCES
Task_Resources = Table(
    "Task_Resources", Base.metadata,
    Column("task_id", Integer, ForeignKey("Tasks.id"), primary_key=True),
    Column("resource_id", Integer, ForeignKey("Users.id"), primary_key=True)
)

# TASK_WATCHERS
Task_Watchers = Table(
    "Task_Watchers", Base.metadata,
    Column("task_id", Integer, ForeignKey("Tasks.id"), primary_key=True),
    Column("watcher_id", Integer, ForeignKey("Users.id"), primary_key=True)
)

# TASK_RESPONSIBLE
Task_Responsible = Table(
    "Task_Responsible", Base.metadata,
    Column("task_id", Integer, ForeignKey("Tasks.id"), primary_key=True),
    Column("responsible_id", Integer, ForeignKey("Users.id"), primary_key=True)
)

# *****************************************************************************
# Register Events
# *****************************************************************************


# *****************************************************************************
# TimeLog updates the owner tasks parents total_logged_seconds attribute
# with new duration
@event.listens_for(TimeLog._start, 'set')
def update_time_log_task_parents_for_start(tlog, new_start, old_start, initiator):
    """Updates the parent task of the task related to the time_log when the
    new_start or end values are changed

    :param tlog: The TimeLog instance
    :param new_start: The datetime.datetime instance showing the new value
    :param old_start: The datetime.datetime instance showing the old value
    :param initiator: not used
    :return: None
    """
    logger.debug('Received set event for new_start in target : %s' % tlog)
    if tlog.end and old_start and new_start:
        old_duration = tlog.end - old_start
        new_duration = tlog.end - new_start
        __update_total_logged_seconds__(tlog, new_duration, old_duration)


@event.listens_for(TimeLog._end, 'set')
def update_time_log_task_parents_for_end(tlog, new_end, old_end, initiator):
    """Updates the parent task of the task related to the time_log when the
    start or new_end values are changed

    :param tlog: The TimeLog instance
    :param new_end: The datetime.datetime instance showing the new value
    :param old_end: The datetime.datetime instance showing the old value
    :param initiator: not used
    :return: None
    """
    logger.debug('received set event for new_end in target : %s' % tlog)
    if tlog.start and old_end and new_end:
        old_duration = old_end - tlog.start
        new_duration = new_end - tlog.start
        __update_total_logged_seconds__(tlog, new_duration, old_duration)


def __update_total_logged_seconds__(tlog, new_duration, old_duration):
    """Updates the given parent tasks total_logged_seconds attribute with the
    new duration

    :param tlog: A :class:`.Task` instance which is the parent of the
    :param new_duration:
    :param old_duration:
    :return:
    """
    if tlog.task:
        logger.debug('TimeLog has a task: %s' % tlog.task)
        parent = tlog.task.parent
        if parent:
            logger.debug('TImeLog.task has a parent: %s' % parent)

            logger.debug('old_duration: %s' % old_duration)
            logger.debug('new_duration: %s' % new_duration)

            old_total_seconds = old_duration.days * 86400 + \
                old_duration.seconds
            new_total_seconds = new_duration.days * 86400 + \
                new_duration.seconds

            parent.total_logged_seconds = \
                parent.total_logged_seconds - old_total_seconds + \
                new_total_seconds
        else:
            logger.debug("TimeLog.task doesn't have a parent:")
    else:
        logger.debug("TimeLog doesn't have a task yet: %s" % tlog)


# *****************************************************************************
# Task.schedule_timing updates Task.parent.schedule_seconds attribute
# *****************************************************************************
@event.listens_for(Task.schedule_timing, 'set', propagate=True)
def update_parents_schedule_seconds_with_schedule_timing(
        task, new_schedule_timing, old_schedule_timing, initiator):
    """Updates the parent tasks schedule_seconds attribute when the
    schedule_timing attribute is updated on a task

    :param task: The base task
    :param new_schedule_timing: an integer showing the schedule_timing of the
      task
    :param old_schedule_timing: the old value of schedule_timing
    :param initiator: not used
    :return: None
    """
    # update parents schedule_seconds attribute
    if task.parent:
        old_schedule_seconds = task.to_seconds(
            old_schedule_timing, task.schedule_unit, task.schedule_model
        )
        new_schedule_seconds = task.to_seconds(
            new_schedule_timing, task.schedule_unit, task.schedule_model
        )
        # remove the old and add the new one
        task.parent.schedule_seconds = \
            task.parent.schedule_seconds - old_schedule_seconds + \
            new_schedule_seconds


# *****************************************************************************
# Task.schedule_unit updates Task.parent.schedule_seconds attribute
# *****************************************************************************
@event.listens_for(Task.schedule_unit, 'set', propagate=True)
def update_parents_schedule_seconds_with_schedule_unit(
        task, new_schedule_unit, old_schedule_unit, initiator):
    """Updates the parent tasks schedule_seconds attribute when the
    new_schedule_unit attribute is updated on a task

    :param task: The base task that the schedule unit is updated of
    :param new_schedule_unit: a string with a value of 'min', 'h', 'd', 'w', 'm' or
      'y' showing the timing unit.
    :param old_schedule_unit: the old value of new_schedule_unit
    :param initiator: not used
    :return: None
    """
    # update parents schedule_seconds attribute
    if task.parent:
        schedule_timing = 0
        if task.schedule_timing:
            schedule_timing = task.schedule_timing
        old_schedule_seconds = task.to_seconds(
            schedule_timing, old_schedule_unit, task.schedule_model
        )
        new_schedule_seconds = task.to_seconds(
            schedule_timing, new_schedule_unit, task.schedule_model
        )
        # remove the old and add the new one
        parent_schedule_seconds = 0
        if task.parent.schedule_seconds:
            parent_schedule_seconds = task.parent.schedule_seconds
        task.parent.schedule_seconds = \
            parent_schedule_seconds - old_schedule_seconds + \
            new_schedule_seconds


# *****************************************************************************
# Task.children removed
# *****************************************************************************
@event.listens_for(Task.children, 'remove', propagate=True)
def update_task_date_values(task, removed_child, initiator):
    """Runs when a child is removed from parent

    :param task: The task that a child is removed from
    :param removed_child: The removed child
    :param initiator: not used
    """
    # update start and end date values of the task
    with DBSession.no_autoflush:
        start = datetime.datetime.max
        end = datetime.datetime.min
        for child in task.children:
            if child is not removed_child:
                if child.start < start:
                    start = child.start
                if child.end > end:
                    end = child.end

        if start != datetime.datetime.max and end != datetime.datetime.min:
            task.start = start
            task.end = end
        else:
            # no child left
            # set it to now
            task.start = datetime.datetime.now()
            # this will also update end
