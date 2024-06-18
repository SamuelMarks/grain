# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for LazyDataset."""

import dataclasses
import sys
import time
from typing import TypeVar, cast
from unittest import mock

from absl import logging
from absl.testing import absltest
from absl.testing import parameterized
from grain._src.core import transforms
import multiprocessing as mp
from grain._src.python import options
from grain._src.python.lazy_dataset import lazy_dataset
from grain._src.python.lazy_dataset.transformations import filter as filter_lazy_dataset
from grain._src.python.lazy_dataset.transformations import map as map_lazy_dataset
from typing_extensions import override


_T = TypeVar('_T')


@dataclasses.dataclass(frozen=True)
class FilterKeepingOddElementsOnly(transforms.FilterTransform):

  def filter(self, element: int) -> bool:
    return bool(element % 2)


class RangeLazyMapDatasetTest(absltest.TestCase):

  def test_len(self):
    ds = lazy_dataset.RangeLazyMapDataset(12)
    self.assertLen(ds, 12)
    ds = lazy_dataset.RangeLazyMapDataset(0, 12)
    self.assertLen(ds, 12)
    ds = lazy_dataset.RangeLazyMapDataset(2, 12)
    self.assertLen(ds, 10)
    ds = lazy_dataset.RangeLazyMapDataset(2, 12, 1)
    self.assertLen(ds, 10)
    ds = lazy_dataset.RangeLazyMapDataset(2, 12, 2)
    self.assertLen(ds, 5)
    ds = lazy_dataset.RangeLazyMapDataset(2, 13, 2)
    self.assertLen(ds, 6)

  def test_getitem(self):
    ds = lazy_dataset.RangeLazyMapDataset(12)
    for i in range(12):
      self.assertEqual(ds[i], i)
    for i in range(12):
      self.assertEqual(ds[i + 12], i)
    ds = lazy_dataset.RangeLazyMapDataset(2, 9, 2)
    self.assertEqual(ds[0], 2)
    self.assertEqual(ds[1], 4)
    self.assertEqual(ds[2], 6)
    self.assertEqual(ds[3], 8)
    self.assertEqual(ds[4], 2)
    self.assertEqual(ds[5], 4)

  def test_iter(self):
    ds = lazy_dataset.RangeLazyMapDataset(12)
    ds_iter = iter(ds)
    elements = [next(ds_iter) for _ in range(12)]
    self.assertEqual(elements, list(range(12)))
    ds = lazy_dataset.RangeLazyMapDataset(2, 9, 2)
    ds_iter = iter(ds)
    elements = [next(ds_iter) for _ in range(4)]
    self.assertEqual(elements, [2, 4, 6, 8])


class PrefetchLazyIterDatasetTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.range_ds = lazy_dataset.RangeLazyMapDataset(20)
    self.filtered_range_ds = filter_lazy_dataset.FilterLazyMapDataset(
        self.range_ds, FilterKeepingOddElementsOnly()
    )
    self.prefetch_lazy_iter_ds = lazy_dataset.PrefetchLazyIterDataset(
        self.range_ds, read_options=options.ReadOptions()
    )

  def test_dataset_and_iterator_types(self):
    self.assertIsInstance(
        self.prefetch_lazy_iter_ds, lazy_dataset.PrefetchLazyIterDataset
    )
    ds_iter = iter(self.prefetch_lazy_iter_ds)
    self.assertIsInstance(ds_iter, lazy_dataset.PrefetchLazyDatasetIterator)

  @parameterized.parameters(0, 1, 10)
  def test_prefetch_data_dense(self, prefetch_buffer_size: int):
    read_options = options.ReadOptions(
        prefetch_buffer_size=prefetch_buffer_size
    )
    prefetch_lazy_iter_ds = lazy_dataset.PrefetchLazyIterDataset(
        self.range_ds, read_options=read_options
    )
    self.assertEqual(prefetch_lazy_iter_ds._read_options, read_options)  # pylint: disable=protected-access
    ds_iter = iter(prefetch_lazy_iter_ds)
    actual = [next(ds_iter) for _ in range(20)]
    expected = list(range(20))
    self.assertSequenceEqual(actual, expected)

  @parameterized.parameters(0, 1, 10)
  def test_prefetch_data_sparse(self, prefetch_buffer_size: int):
    read_options = options.ReadOptions(
        prefetch_buffer_size=prefetch_buffer_size
    )
    prefetch_lazy_iter_ds = lazy_dataset.PrefetchLazyIterDataset(
        self.filtered_range_ds,
        read_options=read_options,
        allow_nones=True,
    )
    self.assertEqual(prefetch_lazy_iter_ds._read_options, read_options)  # pylint: disable=protected-access
    ds_iter = iter(prefetch_lazy_iter_ds)
    actual = [next(ds_iter) for _ in range(20)]
    expected = [i if i % 2 == 1 else None for i in range(20)]
    self.assertSequenceEqual(actual, expected)

  def test_prefetch_iterates_one_epoch(self):
    ds_iter = iter(self.prefetch_lazy_iter_ds)
    _ = [next(ds_iter) for _ in range(20)]
    with self.assertRaises(StopIteration):
      next(ds_iter)

  def test_prefetch_does_not_buffer_unnecessary_elements(self):
    prefetch_buffer_size = 15
    prefetch_lazy_iter_ds_large_buffer = lazy_dataset.PrefetchLazyIterDataset(
        self.range_ds,
        read_options=options.ReadOptions(
            prefetch_buffer_size=prefetch_buffer_size
        ),
    )
    ds_iter = iter(prefetch_lazy_iter_ds_large_buffer)
    self.assertIsInstance(ds_iter, lazy_dataset.PrefetchLazyDatasetIterator)
    ds_iter = cast(lazy_dataset.PrefetchLazyDatasetIterator, ds_iter)
    self.assertIsNone(ds_iter._buffer)
    _ = next(ds_iter)
    self.assertLen(ds_iter._buffer, prefetch_buffer_size)
    _ = [next(ds_iter) for _ in range(14)]
    self.assertLen(
        ds_iter._buffer, len(self.range_ds) - prefetch_buffer_size
    )  # iterated through 15 elements so far
    _ = [next(ds_iter) for _ in range(5)]
    self.assertEmpty(ds_iter._buffer)  # iterated through all elements

  def test_checkpoint(self):
    ds_iter = iter(self.prefetch_lazy_iter_ds)

    max_steps = 20
    values_without_interruption = []
    checkpoints = []
    for _ in range(max_steps):
      checkpoints.append(ds_iter.get_state())  # pytype: disable=attribute-error
      values_without_interruption.append(next(ds_iter))

    for starting_step in [0, 1, 5, 12, 18]:
      ds_iter.set_state(checkpoints[starting_step])  # pytype: disable=attribute-error
      for i in range(starting_step, max_steps):
        value = next(ds_iter)
        self.assertEqual(value, values_without_interruption[i])


class MultiprocessPrefetchLazyIterDatasetTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    ds = lazy_dataset.RangeLazyMapDataset(20)
    ds = lazy_dataset.PrefetchLazyIterDataset(
        ds, read_options=options.ReadOptions()
    )
    self.iter_ds = filter_lazy_dataset.FilterLazyIterDataset(
        ds, FilterKeepingOddElementsOnly()
    )

  @parameterized.named_parameters(
      dict(
          testcase_name='1_worker',
          num_workers=1,
          per_worker_buffer_size=1,
      ),
      dict(
          testcase_name='1_worker_large_buffer',
          num_workers=1,
          per_worker_buffer_size=20,
      ),
      dict(
          testcase_name='10_workers',
          num_workers=10,
          per_worker_buffer_size=1,
      ),
      dict(
          testcase_name='10_workers_large_buffer',
          num_workers=10,
          per_worker_buffer_size=20,
      ),
  )
  def test_prefetch_data(self, num_workers: int, per_worker_buffer_size: int):
    prefetch_lazy_iter_ds = lazy_dataset.MultiprocessPrefetchLazyIterDataset(
        self.iter_ds,
        options.MultiprocessingOptions(num_workers, per_worker_buffer_size),
    )
    actual = list(prefetch_lazy_iter_ds)
    expected = list(range(1, 20, 2))
    self.assertSequenceEqual(actual, expected)

  @parameterized.named_parameters(
      dict(
          testcase_name='1_worker',
          num_workers=7,
          record_state_interval=lazy_dataset._RECORD_STATE_INTERVAL_S,
      ),
      dict(
          testcase_name='10_workers',
          num_workers=10,
          record_state_interval=lazy_dataset._RECORD_STATE_INTERVAL_S,
      ),
      dict(
          testcase_name='10_workers_with_continuous_state_recording',
          num_workers=10,
          record_state_interval=0,
      ),
  )
  def test_checkpoint(self, num_workers: int, record_state_interval: int):
    with mock.patch.object(
        lazy_dataset, '_RECORD_STATE_INTERVAL_S', record_state_interval
    ):
      ds = lazy_dataset.MultiprocessPrefetchLazyIterDataset(
          self.iter_ds,
          options.MultiprocessingOptions(num_workers),
      )
      ds_iter = iter(ds)

      max_steps = 10
      values_without_interruption = []
      checkpoints = []
      for _ in range(max_steps):
        checkpoints.append(ds_iter.get_state())  # pytype: disable=attribute-error
        values_without_interruption.append(next(ds_iter))

      for starting_step in [0, 3, 8]:
        ds_iter.set_state(checkpoints[starting_step])  # pytype: disable=attribute-error
        for i in range(starting_step, max_steps):
          value = next(ds_iter)
          self.assertEqual(value, values_without_interruption[i])

  def test_fails_with_0_workers(self):
    with self.assertRaisesRegex(
        ValueError, '`num_workers` must be greater than 0'
    ):
      lazy_dataset.MultiprocessPrefetchLazyIterDataset(
          self.iter_ds,
          options.MultiprocessingOptions(),
      )

  def test_fails_with_multiple_prefetches(self):
    ds = lazy_dataset.MultiprocessPrefetchLazyIterDataset(
        self.iter_ds,
        options.MultiprocessingOptions(num_workers=10),
    )
    with self.assertRaisesRegex(
        ValueError,
        'Having multiple `MultiprocessPrefetchLazyIterDataset`s is not'
        ' allowed.',
    ):
      _ = lazy_dataset.MultiprocessPrefetchLazyIterDataset(
          ds,
          options.MultiprocessingOptions(num_workers=1),
      )

  @parameterized.product(
      start_prefetch_calls=[0, 1, 10],
      num_workers=[6],
      per_worker_buffer_size=[1, 20],
  )
  def test_start_prefetch(
      self,
      start_prefetch_calls: int,
      num_workers: int,
      per_worker_buffer_size: int,
  ):
    class _SleepTransform(transforms.MapTransform):

      def map(self, features):
        time.sleep(1)
        return features

    dataset = lazy_dataset.RangeLazyMapDataset(10)
    dataset = map_lazy_dataset.MapLazyMapDataset(
        parent=dataset, transform=_SleepTransform()
    )
    dataset = lazy_dataset.PrefetchLazyIterDataset(
        dataset, read_options=options.ReadOptions()
    )
    dataset = lazy_dataset.MultiprocessPrefetchLazyIterDataset(
        dataset,
        options.MultiprocessingOptions(num_workers, per_worker_buffer_size),
    )

    it = iter(dataset)
    assert isinstance(it, lazy_dataset.MultiprocessPrefetchLazyDatasetIterator)
    for _ in range(start_prefetch_calls):
      it.start_prefetch()

    # Waits for prefetching.
    start_time = time.time()
    while time.time() - start_time < 30:
      time.sleep(2)

    # Measures time to read from the dataset.
    start_time = time.time()
    self.assertSequenceEqual(list(it), list(range(10)))

    time_to_fetch = time.time() - start_time
    logging.info('Reading dataset took %.2f seconds.', time_to_fetch)
    if start_prefetch_calls:
      self.assertLess(time_to_fetch, 5)
    else:
      self.assertGreater(time_to_fetch, 1)

  def test_prefetch_but_no_read(self):
    class _SleepTransform(transforms.MapTransform):

      def map(self, features):
        time.sleep(1)
        return features

    dataset = lazy_dataset.RangeLazyMapDataset(10)
    dataset = map_lazy_dataset.MapLazyMapDataset(
        parent=dataset, transform=_SleepTransform()
    )
    dataset = lazy_dataset.PrefetchLazyIterDataset(
        dataset, read_options=options.ReadOptions()
    )
    dataset = lazy_dataset.MultiprocessPrefetchLazyIterDataset(
        dataset,
        options.MultiprocessingOptions(
            num_workers=3, per_worker_buffer_size=20
        ),
    )

    # Makes sure the iterator cleans up gracefully if it is prefetched but no
    # elements are read.
    it = iter(dataset)
    assert isinstance(it, lazy_dataset.MultiprocessPrefetchLazyDatasetIterator)
    it.start_prefetch()
    # Waits for the processes to actually read some elements and put them into
    # buffers.
    time.sleep(30)


class ThreadPrefetchLazyIterDatasetTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.ds = filter_lazy_dataset.FilterLazyIterDataset(
        lazy_dataset.RangeLazyMapDataset(20).to_iter_dataset(),
        FilterKeepingOddElementsOnly(),
    )

  @parameterized.named_parameters(
      dict(
          testcase_name='thread',
          prefetch_buffer_size=1,
          warm_start=True,
      ),
      dict(
          testcase_name='thread_large_buffer',
          prefetch_buffer_size=20,
          warm_start=False,
      ),
      dict(
          testcase_name='thread_huge_buffer',
          prefetch_buffer_size=200,
          warm_start=True,
      ),
  )
  def test_prefetch_data(self, prefetch_buffer_size: int, warm_start: bool):
    prefetch_lazy_iter_ds = lazy_dataset.ThreadPrefetchLazyIterDataset(
        self.ds, prefetch_buffer_size=prefetch_buffer_size
    )
    ds = prefetch_lazy_iter_ds.__iter__()
    if warm_start:
      ds.start_prefetch()
    actual = list(ds)
    expected = list(range(1, 20, 2))
    self.assertSequenceEqual(actual, expected)

  @parameterized.named_parameters(
      dict(
          testcase_name='default_record_state_interval',
          warm_start=False,
      ),
      dict(
          testcase_name='continuous_state_recording',
          warm_start=True,
      ),
  )
  def test_checkpoint(self, warm_start: bool):
    with mock.patch.object(lazy_dataset, '_RECORD_STATE_INTERVAL_S', 0):
      ds = lazy_dataset.ThreadPrefetchLazyIterDataset(
          self.ds,
          prefetch_buffer_size=500,
      )
      ds_iter = ds.__iter__()
      if warm_start:
        ds_iter.start_prefetch()

      max_steps = 10
      values_without_interruption = []
      checkpoints = []
      for _ in range(max_steps):
        checkpoints.append(ds_iter.get_state())  # pytype: disable=attribute-error
        values_without_interruption.append(next(ds_iter))

      for starting_step in range(9):
        ds_iter.set_state(checkpoints[starting_step])  # pytype: disable=attribute-error
        for i in range(starting_step, max_steps):
          value = next(ds_iter)
          self.assertEqual(value, values_without_interruption[i])


class Source15IntsFrom0LazyMapDataset(lazy_dataset.LazyMapDataset[int]):

  def __init__(self):
    super().__init__(parents=[])

  @override
  def __len__(self) -> int:
    return 15

  @override
  def __getitem__(self, index):
    if isinstance(index, slice):
      return self.slice(index)
    return index % len(self)


class Source15IntsFrom0LazyIterDataset(lazy_dataset.LazyIterDataset[int]):

  def __init__(self):
    super().__init__(parents=[])

  @override
  def __iter__(self):
    return iter(range(15))


class IdentityLazyMapDataset(lazy_dataset.LazyMapDataset[_T]):

  def __init__(self, parent: lazy_dataset.LazyMapDataset[_T]):
    super().__init__(parents=parent)

  @override
  def __len__(self) -> int:
    return len(self._parent)

  @override
  def __getitem__(self, index):
    return self._parent[index]


class LazyDatasetTest(parameterized.TestCase):

  def test_parents_source_dataset_has_no_parents(self):
    ds = Source15IntsFrom0LazyMapDataset()
    self.assertEmpty(ds.parents)

  def test_parents_single_source_dataset_has_one_parent(self):
    source_ds = Source15IntsFrom0LazyMapDataset()
    ds = IdentityLazyMapDataset(source_ds)
    self.assertLen(ds.parents, 1)
    self.assertEqual(ds.parents[0], source_ds)

  @parameterized.parameters(
      dict(initial_ds=Source15IntsFrom0LazyMapDataset()),
      dict(initial_ds=Source15IntsFrom0LazyIterDataset()),
  )
  def test_filter_with_callable(self, initial_ds):
    ds = initial_ds.filter(lambda x: x % 2 == 0)
    self.assertSequenceEqual(list(iter(ds)), [0, 2, 4, 6, 8, 10, 12, 14])

  @parameterized.parameters(
      dict(initial_ds=Source15IntsFrom0LazyMapDataset()),
      dict(initial_ds=Source15IntsFrom0LazyIterDataset()),
  )
  def test_filter_with_transform(self, initial_ds):
    ds = initial_ds.filter(FilterKeepingOddElementsOnly())
    self.assertSequenceEqual(list(iter(ds)), [1, 3, 5, 7, 9, 11, 13])

  @parameterized.parameters(
      dict(initial_ds=Source15IntsFrom0LazyMapDataset()),
      dict(initial_ds=Source15IntsFrom0LazyIterDataset()),
  )
  def test_filter_with_callable_and_transform_combined(self, initial_ds):
    ds = initial_ds.filter(lambda x: 3 < x < 10).filter(
        FilterKeepingOddElementsOnly()
    )
    self.assertSequenceEqual(list(iter(ds)), [5, 7, 9])

  @parameterized.parameters(
      dict(initial_ds=Source15IntsFrom0LazyMapDataset()),
      dict(initial_ds=Source15IntsFrom0LazyIterDataset()),
  )
  def test_filter_has_one_parent(self, initial_ds):
    ds = initial_ds.filter(lambda x: True)
    self.assertLen(ds.parents, 1)

  def test_filter_subscription_returns_correct_elements(self):
    ds = Source15IntsFrom0LazyMapDataset().filter(lambda x: x % 2 == 0)
    self.assertSequenceEqual(list(iter(ds)), [0, 2, 4, 6, 8, 10, 12, 14])
    self.assertEqual(ds[0], 0)
    self.assertEqual(ds[12], 12)
    self.assertEqual(ds[8], 8)
    self.assertIsNone(ds[3])
    self.assertIsNone(ds[5])
    self.assertIsNone(ds[13])

  @parameterized.parameters(
      (0),
      (9),
      (30),
  )
  def test_filter_does_not_affect_len(self, ds_length):
    ds = lazy_dataset.RangeLazyMapDataset(ds_length)
    self.assertLen(ds, ds_length)
    ds = ds.filter(lambda x: x % 2 == 0)
    self.assertLen(ds, ds_length)

  @parameterized.named_parameters(
      dict(
          testcase_name='default_args',
          read_options=None,
          allow_nones=False,
          expected=[0, 2, 4, 6, 8, 10, 12, 14],
      ),
      dict(
          testcase_name='custom_read_options',
          read_options=options.ReadOptions(
              num_threads=1, prefetch_buffer_size=1
          ),
          allow_nones=False,
          expected=[0, 2, 4, 6, 8, 10, 12, 14],
      ),
      dict(
          testcase_name='allow_nones',
          read_options=None,
          allow_nones=True,
          expected=[
              0,
              None,
              2,
              None,
              4,
              None,
              6,
              None,
              8,
              None,
              10,
              None,
              12,
              None,
              14,
          ],
      ),
  )
  def test_to_iter_dataset(self, read_options, allow_nones, expected):
    ds = (
        Source15IntsFrom0LazyMapDataset()
        .filter(lambda x: x % 2 == 0)
        .to_iter_dataset(read_options=read_options, allow_nones=allow_nones)
    )
    self.assertSequenceEqual(list(iter(ds)), expected)

  def test_slice_with_just_stop_returns_correct_elements(self):
    ds = Source15IntsFrom0LazyMapDataset().slice(slice(7))
    self.assertSequenceEqual(list(iter(ds)), [0, 1, 2, 3, 4, 5, 6])

  def test_slice_with_start_and_stop_returns_correct_elements(self):
    ds = Source15IntsFrom0LazyMapDataset().slice(slice(3, 9))
    self.assertSequenceEqual(list(iter(ds)), [3, 4, 5, 6, 7, 8])

  def test_slice_with_start_stop_and_step_returns_correct_elements(self):
    ds = Source15IntsFrom0LazyMapDataset().slice(slice(2, 11, 3))
    self.assertSequenceEqual(list(iter(ds)), [2, 5, 8])

  def test_slice_composition_returns_correct_elements(self):
    ds = (
        Source15IntsFrom0LazyMapDataset()
        .slice(slice(1, 10, 2))  # 1, 3, 5, 7, 9
        .slice(slice(1, 3))  # 3, 5
    )
    self.assertSequenceEqual(list(iter(ds)), [3, 5])

  def test_slice_and_filter_composed_returns_correct_elements(self):
    ds = (
        Source15IntsFrom0LazyMapDataset()
        .slice(slice(1, 10, 2))  # 1, 3, 5, 7, 9
        .filter(lambda x: x % 3 == 0 or x == 7)  # None, 3, None, 7, 9
        .filter(lambda x: x > 5)  # None, None, None, 7, 9
        .slice(slice(2, 4))  # None, 7
    )
    self.assertSequenceEqual(list(iter(ds)), [7])

  def test_repeat_updates_length(self):
    ds = Source15IntsFrom0LazyMapDataset().repeat(3)
    self.assertLen(ds, 45)

  def test_repeat_with_none_epochs_updates_length_to_maxsize(self):
    ds = Source15IntsFrom0LazyMapDataset().repeat(num_epochs=None)
    self.assertLen(ds, sys.maxsize)

  def test_repeat_produces_additional_elements_when_iterated(self):
    ds = Source15IntsFrom0LazyMapDataset()[:5].repeat(2)
    self.assertSequenceEqual(list(ds), [0, 1, 2, 3, 4, 0, 1, 2, 3, 4])

  def test_slice_filter_repeat_composed_returns_correct_elements(self):
    ds = (
        Source15IntsFrom0LazyMapDataset()
        .slice(slice(1, 10, 2))  # 1, 3, 5, 7, 9
        .filter(lambda x: x < 6)  # 1, 3, 5, None, None
        .repeat(2)
    )
    self.assertSequenceEqual(list(ds), [1, 3, 5, 1, 3, 5])


if __name__ == '__main__':
  absltest.main()
