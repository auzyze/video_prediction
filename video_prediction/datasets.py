import glob
import itertools
import os
import re
from collections import OrderedDict

import numpy as np
import tensorflow as tf
from tensorflow.contrib.training import HParams
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.data.util import nest
from tensorflow.python.framework import tensor_shape


class _FixedBatchDataset(dataset_ops.BatchDataset):
    @property
    def output_shapes(self):
        input_shapes = self._input_dataset.output_shapes
        return nest.pack_sequence_as(input_shapes, [
            tensor_shape.vector(self._batch_size).concatenate(s)
            for s in nest.flatten(self._input_dataset.output_shapes)
        ])


class VideoDataset:
    def __init__(self, input_dir, mode='train', num_epochs=None,
                 hparams_dict=None, hparams=None):
        """
        Args:
            input_dir: either a directory containing subdirectories train,
                val, test, etc, or a directory containing the tfrecords.
            mode: either train, val, or test
            num_epochs: if None, dataset is iterated indefinitely.
            hparams_dict: a dict of `name=value` pairs, where `name` must be
                defined in `self.get_default_hparams()`.
            hparams: a string of comma separated list of `name=value` pairs,
                where `name` must be defined in `self.get_default_hparams()`.
                These values overrides any values in hparams_dict (if any).

        Note:
            self.input_dir is the directory containing the tfrecords.
        """
        self.input_dir = os.path.normpath(os.path.expanduser(input_dir))
        self.mode = mode
        self.num_epochs = num_epochs

        if self.mode not in ('train', 'val', 'test'):
            raise ValueError('Invalid mode %s' % self.mode)

        if not os.path.exists(self.input_dir):
            raise FileNotFoundError("input_dir %s does not exist" % self.input_dir)
        self.filenames = None
        # look for tfrecords in input_dir and input_dir/mode directories
        for input_dir in [self.input_dir, os.path.join(self.input_dir, self.mode)]:
            filenames = glob.glob(os.path.join(input_dir, '*.tfrecord*'))
            if filenames:
                self.input_dir = input_dir
                self.filenames = filenames
                break
        if not self.filenames:
            raise FileNotFoundError('No tfrecords were found in %s.' % self.input_dir)
        self.dataset_name = os.path.basename(os.path.split(self.input_dir)[0])

        self.state_like_names_and_shapes = OrderedDict()
        self.action_like_names_and_shapes = OrderedDict()
        self._max_sequence_length = None
        self._dict_message = None

        self.hparams = self.get_default_hparams().set_from_map(hparams_dict or {}).parse(hparams or '')

    def get_default_hparams_dict(self):
        """
        Returns:
            A dict with the following hyperparameters.

            crop_size: crop image into a square with sides of this length.
            scale_size: resize image to this size after it has been cropped.
            context_frames: the number of ground-truth frames to pass in at
                start.
            sequence_length: the number of frames in the video sequence, so
                state-like sequences are of length sequence_length and
                action-like sequences are of length sequence_length - 1.
                This number includes the context frames.
            frame_skip: number of frames to skip in between outputted frames,
                so frame_skip=0 denotes no skipping.
            time_shift: shift in time by multiples of this, so time_shift=1
                denotes all possible shifts. time_shift=0 denotes no shifting.
                It is ignored (equiv. to time_shift=0) when mode != 'train'.
            use_state: whether to load and return state and actions.
        """
        hparams = dict(
            crop_size=0,
            scale_size=0,
            context_frames=1,
            sequence_length=0,
            frame_skip=0,
            time_shift=1,
            use_state=True,
        )
        return hparams

    def get_default_hparams(self):
        return HParams(**self.get_default_hparams_dict())

    @property
    def jpeg_encoding(self):
        raise NotImplementedError

    def _check_or_infer_shapes(self):
        """
        Should be called after state_like_names_and_shapes and
        action_like_names_and_shapes have been finalized.
        """
        state_like_names_and_shapes = OrderedDict([(k, list(v)) for k, v in self.state_like_names_and_shapes.items()])
        action_like_names_and_shapes = OrderedDict([(k, list(v)) for k, v in self.action_like_names_and_shapes.items()])
        from google.protobuf.json_format import MessageToDict
        example = next(tf.python_io.tf_record_iterator(self.filenames[0]))
        self._dict_message = MessageToDict(tf.train.Example.FromString(example))
        for example_name, name_and_shape in (list(state_like_names_and_shapes.items()) +
                                             list(action_like_names_and_shapes.items())):
            name, shape = name_and_shape
            feature = self._dict_message['features']['feature']
            names = [name_ for name_ in feature.keys() if re.search(name.replace('%d', '(\d+)'), name_) is not None]
            if example_name in self.state_like_names_and_shapes:
                sequence_length = len(names)
            else:
                sequence_length = len(names) + 1
            if self._max_sequence_length is None:
                self._max_sequence_length = sequence_length
            else:
                self._max_sequence_length = min(sequence_length, self._max_sequence_length)
            name = names[0]
            feature = feature[name]
            list_type, = feature.keys()
            if list_type == 'floatList':
                inferred_shape = (len(feature[list_type]['value']),)
                if shape is None:
                    name_and_shape[1] = inferred_shape
                else:
                    if inferred_shape != shape:
                        raise ValueError('Inferred shape for feature %s is %r but instead got shape %r.' %
                                         (name, inferred_shape, shape))
            elif list_type == 'bytesList':
                image_str, = feature[list_type]['value']
                # try to infer image shape
                inferred_shape = None
                if not self.jpeg_encoding:
                    spatial_size = len(image_str) // 4
                    height = width = int(np.sqrt(spatial_size))  # assume square image
                    if len(image_str) == (height * width * 4):
                        inferred_shape = (height, width, 3)
                if shape is None:
                    if inferred_shape is not None:
                        name_and_shape[1] = inferred_shape
                    else:
                        raise ValueError('Unable to infer shape for feature %s of size %d.' % (name, len(image_str)))
                else:
                    if inferred_shape is not None and inferred_shape != shape:
                        raise ValueError('Inferred shape for feature %s is %r but instead got shape %r.' %
                                         (name, inferred_shape, shape))
            else:
                raise NotImplementedError
        self.state_like_names_and_shapes = OrderedDict([(k, tuple(v)) for k, v in state_like_names_and_shapes.items()])
        self.action_like_names_and_shapes = OrderedDict([(k, tuple(v)) for k, v in action_like_names_and_shapes.items()])

    def parser(self, serialized_example):
        """Parses a single tf.Example into images, states, actions, etc tensors."""
        features = dict()
        for i in range(self._max_sequence_length):
            for example_name, (name, shape) in self.state_like_names_and_shapes.items():
                if example_name == 'images':  # special handling for image
                    features[name % i] = tf.FixedLenFeature([1], tf.string)
                else:
                    features[name % i] = tf.FixedLenFeature(shape, tf.float32)
        for i in range(self._max_sequence_length - 1):
            for example_name, (name, shape) in self.action_like_names_and_shapes.items():
                features[name % i] = tf.FixedLenFeature(shape, tf.float32)

        # check that the features are in the tfrecord
        for name in features.keys():
            if name not in self._dict_message['features']['feature']:
                raise ValueError('Feature with name %s not found in tfrecord. Possible feature names are:\n%s' %
                                 (name, '\n'.join(sorted(self._dict_message['features']['feature'].keys()))))

        # parse all the features of all time steps together
        features = tf.parse_single_example(serialized_example, features=features)

        state_like_seqs = OrderedDict([(example_name, []) for example_name in self.state_like_names_and_shapes])
        action_like_seqs = OrderedDict([(example_name, []) for example_name in self.action_like_names_and_shapes])
        for i in range(self._max_sequence_length):
            for example_name, (name, shape) in self.state_like_names_and_shapes.items():
                if example_name == 'images':  # special handling for image
                    if self.jpeg_encoding:
                        image_buffer = tf.reshape(features[name % i], shape=[])
                        image = tf.image.decode_jpeg(image_buffer, channels=shape[-1])
                    else:
                        image = tf.decode_raw(features[name % i], tf.uint8)
                    image = tf.image.convert_image_dtype(image, dtype=tf.float32)
                    image = tf.reshape(image, shape)
                    image = self.preprocess_image(image)
                    state_like_seqs[example_name].append(image)
                else:
                    state_like_seqs[example_name].append(features[name % i])
        for i in range(self._max_sequence_length - 1):
            for example_name, (name, shape) in self.action_like_names_and_shapes.items():
                action_like_seqs[example_name].append(features[name % i])

        # set sequence_length to the longest possible if it is not specified
        if not self.hparams.sequence_length:
            self.hparams.sequence_length = (self._max_sequence_length - 1) // (self.hparams.frame_skip + 1) + 1

        # handle random shifting and frame skip
        sequence_length = self.hparams.sequence_length
        frame_skip = self.hparams.frame_skip
        time_shift = self.hparams.time_shift
        if time_shift and self.mode == 'train':
            assert time_shift > 0 and isinstance(time_shift, int)
            num_shifts = ((self._max_sequence_length - 1) - (sequence_length - 1) * (frame_skip + 1)) // time_shift
            if num_shifts < 0:
                raise ValueError('max_sequence_length has to be at least %d when '
                                 'sequence_length=%d, frame_skip=%d, but '
                                 'instead it is %d' %
                                 ((sequence_length - 1) * (frame_skip + 1) + 1,
                                  sequence_length, frame_skip, self._max_sequence_length))
            t_start = tf.random_uniform([], 0, num_shifts + 1, dtype=tf.int32) * time_shift
        else:
            t_start = 0
        state_like_t_slice = slice(t_start, t_start + (sequence_length - 1) * (frame_skip + 1) + 1, frame_skip + 1)
        action_like_t_slice = slice(t_start, t_start + (sequence_length - 1) * (frame_skip + 1))

        for example_name, seq in state_like_seqs.items():
            seq = tf.stack(seq)[state_like_t_slice]
            seq.set_shape([sequence_length] + seq.shape.as_list()[1:])
            state_like_seqs[example_name] = seq
        for example_name, seq in action_like_seqs.items():
            seq = tf.stack(seq)[action_like_t_slice]
            seq.set_shape([(sequence_length - 1) * (frame_skip + 1)] + seq.shape.as_list()[1:])
            # concatenate actions of skipped frames into single macro actions
            seq = tf.reshape(seq, [sequence_length - 1, -1])
            action_like_seqs[example_name] = seq

        return state_like_seqs, action_like_seqs

    def make_batch(self, batch_size):
        filenames = self.filenames
        dataset = tf.data.TFRecordDataset(filenames).repeat(self.num_epochs)

        self._check_or_infer_shapes()

        dataset = dataset.map(self.parser, num_parallel_calls=batch_size)
        dataset.prefetch(2 * batch_size)

        # Potentially shuffle records.
        if self.mode == 'train':
            # min_queue_examples = int(
            #     self.num_examples_per_epoch() * 0.4)
            # # Ensure that the capacity is sufficiently large to provide good random
            # # shuffling.
            # dataset = dataset.shuffle(buffer_size=min_queue_examples + 3 * batch_size)
            dataset = dataset.shuffle(buffer_size=4096)

        dataset = _FixedBatchDataset(dataset, batch_size)
        iterator = dataset.make_one_shot_iterator()
        state_like_batches, action_like_batches = iterator.get_next()

        input_batches = OrderedDict(list(state_like_batches.items()) + list(action_like_batches.items()))
        target_batches = state_like_batches['images'][:, self.hparams.context_frames:]
        return input_batches, target_batches

    def preprocess_image(self, image):
        """Preprocess a single image in [height, width, depth] layout."""
        image_shape = image.get_shape().as_list()
        crop_size = self.hparams.crop_size
        scale_size = self.hparams.scale_size
        if not crop_size:
            crop_size = min(image_shape[0], image_shape[1])
        image = tf.image.resize_image_with_crop_or_pad(image, crop_size, crop_size)
        image = tf.reshape(image, [crop_size, crop_size, 3])
        if scale_size:
            # upsample with bilinear interpolation but downsample with area interpolation
            if crop_size < scale_size:
                image = tf.image.resize_images(image, [scale_size, scale_size],
                                               method=tf.image.ResizeMethod.BILINEAR)
            elif crop_size > scale_size:
                image = tf.image.resize_images(image, [scale_size, scale_size],
                                               method=tf.image.ResizeMethod.AREA)
            else:
                # image remains unchanged
                pass
        return image

    def num_examples_per_epoch(self):
        # extract information from filename to count the number of trajectories in the dataset
        count = 0
        for filename in self.filenames:
            match = re.search('traj_(\d+)_to_(\d+).tfrecords', os.path.basename(filename))
            start_traj_iter = int(match.group(1))
            end_traj_iter = int(match.group(2))
            count += end_traj_iter - start_traj_iter + 1

        # alternatively, the dataset size can be determined like this, but it's very slow
        # count = sum(sum(1 for _ in tf.python_io.tf_record_iterator(filename)) for filename in filenames)
        return count


class GoogleRobotVideoDataset(VideoDataset):
    """
    https://sites.google.com/site/brainrobotdata/home/push-dataset
    """
    def __init__(self, *args, **kwargs):
        super(GoogleRobotVideoDataset, self).__init__(*args, **kwargs)
        self.state_like_names_and_shapes['images'] = 'move/%d/image/encoded', (512, 640, 3)
        if self.hparams.use_state:
            self.state_like_names_and_shapes['states'] = 'move/%d/endeffector/vec_pitch_yaw', (5,)
            self.action_like_names_and_shapes['actions'] = 'move/%d/commanded_pose/vec_pitch_yaw', (5,)

    def get_default_hparams_dict(self):
        default_hparams = super(GoogleRobotVideoDataset, self).get_default_hparams_dict()
        hparams = dict(
            context_frames=2,
            sequence_length=15,
        )
        return dict(itertools.chain(default_hparams.items(), hparams.items()))

    def num_examples_per_epoch(self):
        if os.path.basename(self.input_dir) == 'push_train':
            count = 51615
        elif os.path.basename(self.input_dir) == 'push_testseen':
            count = 1038
        elif os.path.basename(self.input_dir) == 'push_testnovel':
            count = 995
        else:
            raise NotImplementedError
        return count

    @property
    def jpeg_encoding(self):
        return True


class SV2PVideoDataset(VideoDataset):
    def __init__(self, *args, **kwargs):
        super(SV2PVideoDataset, self).__init__(*args, **kwargs)
        self.dataset_name = os.path.basename(os.path.split(self.input_dir)[0])
        self.state_like_names_and_shapes['images'] = 'image_%d', (64, 64, 3)
        if self.dataset_name == 'shape':
            if self.hparams.use_state:
                self.state_like_names_and_shapes['states'] = 'state_%d', (2,)
                self.action_like_names_and_shapes['actions'] = 'action_%d', (2,)
        elif self.dataset_name == 'humans':
            if self.hparams.use_state:
                raise ValueError('SV2PVideoDataset does not have states, use_state should be False')
        else:
            raise NotImplementedError

    def get_default_hparams_dict(self):
        default_hparams = super(SV2PVideoDataset, self).get_default_hparams_dict()
        if self.dataset_name == 'shape':
            hparams = dict()
        elif self.dataset_name == 'humans':
            hparams = dict(
                context_frames=10,
                sequence_length=20,
                use_state=False,
            )
        else:
            raise NotImplementedError
        return dict(itertools.chain(default_hparams.items(), hparams.items()))

    def num_examples_per_epoch(self):
        if self.dataset_name == 'shape':
            if os.path.basename(self.input_dir) == 'train':
                count = 43415
            elif os.path.basename(self.input_dir) == 'val':
                count = 2898
            else:  # shape dataset doesn't have a test set
                raise NotImplementedError
        elif self.dataset_name == 'humans':
            if os.path.basename(self.input_dir) == 'train':
                count = 23910
            elif os.path.basename(self.input_dir) == 'val':
                count = 10472
            elif os.path.basename(self.input_dir) == 'test':
                count = 7722
            else:
                raise NotImplementedError
        else:
            raise NotImplementedError
        return count

    @property
    def jpeg_encoding(self):
        return True


class SoftmotionVideoDataset(VideoDataset):
    """
    https://sites.google.com/view/sna-visual-mpc
    """
    def __init__(self, *args, **kwargs):
        super(SoftmotionVideoDataset, self).__init__(*args, **kwargs)
        self.state_like_names_and_shapes['images'] = '%d/image_view0/encoded', (64, 64, 3)
        if self.hparams.use_state:
            self.state_like_names_and_shapes['states'] = '%d/endeffector_pos', (3,)
            self.action_like_names_and_shapes['actions'] = '%d/action', (4,)
        if os.path.basename(self.input_dir).endswith('annotation'):
            self.state_like_names_and_shapes['object_pos'] = '%d/object_pos', (2,)

    def get_default_hparams_dict(self):
        default_hparams = super(SoftmotionVideoDataset, self).get_default_hparams_dict()
        hparams = dict(
            context_frames=2,
            sequence_length=15,
            time_shift=2,
        )
        return dict(itertools.chain(default_hparams.items(), hparams.items()))

    @property
    def jpeg_encoding(self):
        return False


if __name__ == '__main__':
    import cv2

    datasets = [
        GoogleRobotVideoDataset('data/push/push_testseen', mode='test'),
        SV2PVideoDataset('data/shape', mode='val'),
        SV2PVideoDataset('data/humans', mode='val'),
        SoftmotionVideoDataset('data/softmotion30_v1', mode='val'),
    ]
    batch_size = 4

    sess = tf.Session()

    for dataset in datasets:
        inputs, _ = dataset.make_batch(batch_size)
        images = inputs['images']
        images = tf.reshape(images, [-1] + images.get_shape().as_list()[2:])
        images = sess.run(images)
        images = (images * 255).astype(np.uint8)
        for image in images:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            cv2.imshow(dataset.input_dir, image)
            cv2.waitKey(50)
