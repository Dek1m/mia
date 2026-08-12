"""Unit-тесты для SharedMemoryManager."""
import multiprocessing.shared_memory as shm

import pytest

from shared_memory import SharedMemoryManager


# === Фикстуры ===

@pytest.fixture
def manager():
    """Создаёт SharedMemoryManager и чистит после теста."""
    m = SharedMemoryManager()
    yield m
    m.cleanup()


# === Базовые тесты ===

def test_create_segment(manager):
    """create() создаёт сегмент разделяемой памяти."""
    segment = manager.create("test_seg_1", size=1024)
    assert segment is not None
    assert isinstance(segment, shm.SharedMemory)
    assert segment.size == 1024
    assert "test_seg_1" in manager._segments
    # Закрыть отдельно, cleanup тоже попытается
    segment.close()


def test_attach_segment():
    """attach() подключается к существующему сегменту."""
    # Создаём через собственный менеджер, а аттачим через другой
    creator = SharedMemoryManager()
    original = creator.create("test_attach_seg", size=256)

    attacher = SharedMemoryManager()
    try:
        attached = attacher.attach("test_attach_seg")
        assert attached is not None
        assert isinstance(attached, shm.SharedMemory)
        assert "test_attach_seg" in attacher._segments
        attached.close()
    finally:
        creator.cleanup()


def test_cleanup(manager):
    """cleanup() удаляет все сегменты и очищает словарь."""
    manager.create("test_cleanup_1", size=512)
    manager.create("test_cleanup_2", size=1024)
    assert len(manager._segments) == 2

    manager.cleanup()
    assert len(manager._segments) == 0


def test_cleanup_after_create(manager):
    """cleanup после create удаляет файл сегмента."""
    segment = manager.create("test_cleanup_file", size=256)
    name = segment.name
    segment.close()

    manager.cleanup()

    # Проверяем что сегмент удалён — повторное подключение должно упасть
    with pytest.raises(FileNotFoundError):
        shm.SharedMemory(name=name)


def test_multiple_segments(manager):
    """Несколько сегментов создаются и работают параллельно."""
    seg1 = manager.create("multi_seg_1", size=128)
    seg2 = manager.create("multi_seg_2", size=256)
    seg3 = manager.create("multi_seg_3", size=512)

    assert len(manager._segments) == 3
    assert seg1.size == 128
    assert seg2.size == 256
    assert seg3.size == 512

    # Каждый сегмент доступен по имени
    assert manager._segments["multi_seg_1"] is seg1
    assert manager._segments["multi_seg_2"] is seg2
    assert manager._segments["multi_seg_3"] is seg3

    seg1.close()
    seg2.close()
    seg3.close()


def test_segment_data_write_read(manager):
    """Запись и чтение данных через разделяемую память."""
    data = b"hello shared memory!"
    segment = manager.create("test_rw_seg", size=len(data))

    # Записать данные в буфер
    segment.buf[:len(data)] = data

    # Прочитать данные обратно
    result = bytes(segment.buf[:len(data)])
    assert result == data

    segment.close()
