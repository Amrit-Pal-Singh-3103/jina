from math import sqrt, ceil, floor
from typing import Optional, Union, Callable, Tuple, Sequence, TYPE_CHECKING

import numpy as np

from ... import Document
from ...helper import deprecated_method
from ...importer import ImportExtensions
from ...logging.profile import ProgressBar
from ...math.helper import top_k, minmax_normalize, update_rows_x_mat_best

if TYPE_CHECKING:
    from .document import DocumentArray
    from .memmap import DocumentArrayMemmap


class DocumentArrayNeuralOpsMixin:
    """ A mixin that provides match functionality to DocumentArrays """

    def match(
        self,
        darray: Union['DocumentArray', 'DocumentArrayMemmap'],
        metric: Union[
            str, Callable[['np.ndarray', 'np.ndarray'], 'np.ndarray']
        ] = 'cosine',
        limit: Optional[Union[int, float]] = 20,
        normalization: Optional[Tuple[float, float]] = None,
        metric_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        traversal_ldarray: Optional[Sequence[str]] = None,
        traversal_rdarray: Optional[Sequence[str]] = None,
        use_scipy: bool = False,
        exclude_self: bool = False,
        is_sparse: bool = False,
        filter_fn: Optional[Callable[['Document'], bool]] = None,
        only_id: bool = False,
    ) -> None:
        """Compute embedding based nearest neighbour in `another` for each Document in `self`,
        and store results in `matches`.
        .. note::
            'cosine', 'euclidean', 'sqeuclidean' are supported natively without extra dependency.
            You can use other distance metric provided by ``scipy``, such as `braycurtis`, `canberra`, `chebyshev`,
            `cityblock`, `correlation`, `cosine`, `dice`, `euclidean`, `hamming`, `jaccard`, `jensenshannon`,
            `kulsinski`, `mahalanobis`, `matching`, `minkowski`, `rogerstanimoto`, `russellrao`, `seuclidean`,
            `sokalmichener`, `sokalsneath`, `sqeuclidean`, `wminkowski`, `yule`.
            To use scipy metric, please set ``use_scipy=True``.
        - To make all matches values in [0, 1], use ``dA.match(dB, normalization=(0, 1))``
        - To invert the distance as score and make all values in range [0, 1],
            use ``dA.match(dB, normalization=(1, 0))``. Note, how ``normalization`` differs from the previous.
        :param darray: the other DocumentArray or DocumentArrayMemmap to match against
        :param metric: the distance metric
        :param limit: the maximum number of matches, when not given defaults to 20.
        :param normalization: a tuple [a, b] to be used with min-max normalization,
                                the min distance will be rescaled to `a`, the max distance will be rescaled to `b`
                                all values will be rescaled into range `[a, b]`.
        :param metric_name: if provided, then match result will be marked with this string.
        :param batch_size: if provided, then `darray` is loaded in chunks of, at most, batch_size elements. This option
                           will be slower but more memory efficient. Specialy indicated if `darray` is a big
                           DocumentArrayMemmap.
        :param traversal_ldarray: if set, then matching is applied along the `traversal_path` of the
                left-hand ``DocumentArray``.
        :param traversal_rdarray: if set, then matching is applied along the `traversal_path` of the
                right-hand ``DocumentArray``.
        :param filter_fn: if set, apply the filter function to filter docs on the right hand side (rhv) to be matched
        :param use_scipy: if set, use ``scipy`` as the computation backend
        :param exclude_self: if set, Documents in ``darray`` with same ``id`` as the left-hand values will not be
                        considered as matches.
        :param is_sparse: if set, the embeddings of left & right DocumentArray are considered as sparse NdArray
        :param only_id: if set, then returning matches will only contain ``id``
        """
        if limit is not None:
            if limit <= 0:
                raise ValueError(f'`limit` must be larger than 0, receiving {limit}')
            else:
                limit = int(limit)

        if batch_size is not None:
            if batch_size <= 0:
                raise ValueError(
                    f'`batch_size` must be larger than 0, receiving {batch_size}'
                )
            else:
                batch_size = int(batch_size)

        lhv = self
        if traversal_ldarray:
            lhv = self.traverse_flat(traversal_ldarray)

            from .document import DocumentArray

            if not isinstance(lhv, DocumentArray):
                lhv = DocumentArray(lhv)

        rhv = darray
        if traversal_rdarray or filter_fn:
            rhv = darray.traverse_flat(traversal_rdarray or ['r'], filter_fn=filter_fn)

            from .document import DocumentArray

            if not isinstance(rhv, DocumentArray):
                rhv = DocumentArray(rhv)

        if not (lhv and rhv):
            return

        if callable(metric):
            cdist = metric
        elif isinstance(metric, str):
            if use_scipy:
                from scipy.spatial.distance import cdist as cdist
            else:
                from ...math.distance import cdist as cdist
        else:
            raise TypeError(
                f'metric must be either string or a 2-arity function, received: {metric!r}'
            )

        metric_name = metric_name or (metric.__name__ if callable(metric) else metric)
        _limit = len(rhv) if limit is None else (limit + (1 if exclude_self else 0))

        if batch_size:
            dist, idx = lhv._match_online(
                rhv, cdist, _limit, normalization, metric_name, batch_size
            )
        else:
            dist, idx = lhv._match(
                rhv, cdist, _limit, normalization, metric_name, is_sparse
            )

        def _get_id_from_da(rhv, int_offset):
            return rhv._pb_body[int_offset].id

        def _get_id_from_dam(rhv, int_offset):
            return rhv._int2str_id(int_offset)

        from .memmap import DocumentArrayMemmap

        if isinstance(rhv, DocumentArrayMemmap):
            _get_id = _get_id_from_dam
        else:
            _get_id = _get_id_from_da

        for _q, _ids, _dists in zip(lhv, idx, dist):
            _q.matches.clear()
            num_matches = 0
            for _id, _dist in zip(_ids, _dists):
                # Note, when match self with other, or both of them share the same Document
                # we might have recursive matches .
                # checkout https://github.com/jina-ai/jina/issues/3034
                if only_id:
                    d = Document(id=_get_id(rhv, _id))
                else:
                    d = rhv[int(_id)]  # type: Document

                if d.id in lhv:
                    d = Document(d, copy=True)
                    d.pop('matches')
                if not (d.id == _q.id and exclude_self):
                    _q.matches.append(d, scores={metric_name: _dist})
                    num_matches += 1
                    if num_matches >= (limit or _limit):
                        break

    def _match(self, darray, cdist, limit, normalization, metric_name, is_sparse):
        """
        Computes the matches between self and `darray` loading `darray` into main memory.
        :param darray: the other DocumentArray or DocumentArrayMemmap to match against
        :param cdist: the distance metric
        :param limit: the maximum number of matches, when not given
                      all Documents in `darray` are considered as matches
        :param normalization: a tuple [a, b] to be used with min-max normalization,
                                the min distance will be rescaled to `a`, the max distance will be rescaled to `b`
                                all values will be rescaled into range `[a, b]`.
        :param metric_name: if provided, then match result will be marked with this string.
        :param is_sparse: if provided, then match is computed on sparse embeddings
        :return: distances and indices
        """

        if is_sparse:
            import scipy.sparse as sp

            x_mat = sp.vstack(self.get_attributes('embedding'))
            y_mat = sp.vstack(darray.get_attributes('embedding'))
            dists = cdist(x_mat, y_mat, metric_name, is_sparse=True)
        else:
            x_mat = self.embeddings
            y_mat = darray.embeddings
            dists = cdist(x_mat, y_mat, metric_name)

        dist, idx = top_k(dists, min(limit, len(darray)), descending=False)
        if isinstance(normalization, (tuple, list)) and normalization is not None:

            # normalization bound uses original distance not the top-k trimmed distance
            if is_sparse:
                min_d = dists.min(axis=-1).toarray()
                max_d = dists.max(axis=-1).toarray()
            else:
                min_d = np.min(dists, axis=-1, keepdims=True)
                max_d = np.max(dists, axis=-1, keepdims=True)

            dist = minmax_normalize(dist, normalization, (min_d, max_d))

        return dist, idx

    def _match_online(
        self, darray, cdist, limit, normalization, metric_name, batch_size
    ):
        """
        Computes the matches between self and `darray` loading `darray` into main memory in chunks of size `batch_size`.

        :param darray: the other DocumentArray or DocumentArrayMemmap to match against
        :param cdist: the distance metric
        :param limit: the maximum number of matches, when not given
                      all Documents in `another` are considered as matches
        :param normalization: a tuple [a, b] to be used with min-max normalization,
                              the min distance will be rescaled to `a`, the max distance will be rescaled to `b`
                              all values will be rescaled into range `[a, b]`.
        :param batch_size: length of the chunks loaded into memory from darray.
        :param metric_name: if provided, then match result will be marked with this string.
        :return: distances and indices
        """
        assert isinstance(
            darray[0].embedding, np.ndarray
        ), f'expected embedding of type np.ndarray but received {type(darray[0].embedding)}'

        x_mat = self.embeddings
        n_x = x_mat.shape[0]

        def batch_generator(y_darray: 'DocumentArrayMemmap', n_batch: int):
            for i in range(0, len(y_darray), n_batch):
                y_mat = y_darray._get_embeddings(slice(i, i + n_batch))
                yield y_mat, i

        y_batch_generator = batch_generator(darray, batch_size)
        top_dists = np.inf * np.ones((n_x, limit))
        top_inds = np.zeros((n_x, limit), dtype=int)

        for y_batch, y_batch_start_pos in y_batch_generator:
            distances = cdist(x_mat, y_batch, metric_name)
            dists, inds = top_k(distances, limit, descending=False)

            if isinstance(normalization, (tuple, list)) and normalization is not None:
                dists = minmax_normalize(dists, normalization)

            inds = y_batch_start_pos + inds
            top_dists, top_inds = update_rows_x_mat_best(
                top_dists, top_inds, dists, inds, limit
            )

        # sort final the final `top_dists` and `top_inds` per row
        permutation = np.argsort(top_dists, axis=1)
        dist = np.take_along_axis(top_dists, permutation, axis=1)
        idx = np.take_along_axis(top_inds, permutation, axis=1)

        return dist, idx

    @deprecated_method(new_function_name='plot_embeddings')
    def visualize(self, *args, **kwargs):
        """Deprecated! Please use :meth:`.plot_embeddings` instead.

        Plot embeddings in a 2D projection with the PCA algorithm. This function requires ``matplotlib`` installed.

        :param args: extra args
        :param kwargs: extra kwargs
        """
        self.plot_embeddings(*args, **kwargs)

    def plot_embeddings(
        self,
        output: Optional[str] = None,
        title: Optional[str] = None,
        colored_attr: Optional[str] = None,
        colormap: str = 'rainbow',
        method: str = 'pca',
        show_axis: bool = False,
        **kwargs,
    ):
        """Plot embeddings in a 2D projection with the PCA algorithm. This function requires ``matplotlib`` installed.

        If `tag_name` is provided the plot uses a distinct color for each unique tag value in the
        documents of the DocumentArray.

        :param output: Optional path to store the visualization. If not given, show in UI
        :param title: Optional title of the plot. When not given, the default title is used.
        :param colored_attr: Optional str that specifies attribute used to color the plot, it supports dunder expression
            such as `tags__label`, `matches__0__id`.
        :param colormap: the colormap string supported by matplotlib.
        :param method: the visualization method, available `pca`, `tsne`. `pca` is fast but may not well represent
                nonlinear relationship of high-dimensional data. `tsne` requires scikit-learn to be installed and is
                much slower.
        :param show_axis: If set, axis and bounding box of the plot will be printed.
        :param kwargs: extra kwargs pass to matplotlib.plot
        """

        x_mat = self.embeddings
        assert isinstance(
            x_mat, np.ndarray
        ), f'Type {type(x_mat)} not currently supported, use np.ndarray embeddings'

        if method == 'tsne':
            from sklearn.manifold import TSNE

            x_mat_2d = TSNE(n_components=2).fit_transform(x_mat)
        else:
            from ...math.dimensionality_reduction import PCA

            x_mat_2d = PCA(n_components=2).fit_transform(x_mat)

        plt_kwargs = {
            'x': x_mat_2d[:, 0],
            'y': x_mat_2d[:, 1],
            'alpha': 0.2,
            'marker': '.',
        }

        with ImportExtensions(required=True):
            import matplotlib.pyplot as plt

        plt.figure(figsize=(8, 8))

        plt.title(title or f'{len(x_mat)} Documents with {method}')

        if colored_attr:
            tags = []

            for x in self:
                try:
                    tags.append(getattr(x, colored_attr))
                except (KeyError, AttributeError, ValueError):
                    tags.append(None)
            tag_to_num = {tag: num for num, tag in enumerate(set(tags))}
            plt_kwargs['c'] = np.array([tag_to_num[ni] for ni in tags])
            plt_kwargs['cmap'] = plt.get_cmap(colormap)

        # update the plt_kwargs
        plt_kwargs.update(kwargs)

        plt.scatter(**plt_kwargs)

        if not show_axis:
            plt.gca().set_axis_off()
            plt.gca().xaxis.set_major_locator(plt.NullLocator())
            plt.gca().yaxis.set_major_locator(plt.NullLocator())

        if output:
            plt.savefig(output, bbox_inches='tight', pad_inches=0.1)
        else:
            plt.show()

    def _get_embeddings(self, indices: Optional[slice] = None) -> np.ndarray:
        """Return a `np.ndarray` stacking  the `embedding` attributes as rows.
        If indices is passed the embeddings from the indices are retrieved, otherwise
        all indices are retrieved.

        Example: `self._get_embeddings(10:20)` will return 10 embeddings from positions 10 to 20
                  in the `DocumentArray` or `DocumentArrayMemmap`

        .. warning:: This operation assumes all embeddings have the same shape and dtype.
                 All dtype and shape values are assumed to be equal to the values of the
                 first element in the DocumentArray / DocumentArrayMemmap

        .. warning:: This operation currently does not support sparse arrays.

        :param indices: slice of data from where to retrieve embeddings.
        :return: embeddings stacked per row as `np.ndarray`.
        """
        if indices is None:
            indices = slice(0, len(self))

        x_mat = bytearray()
        len_slice = 0
        for d in self[indices]:
            x_mat += d.proto.embedding.dense.buffer
            len_slice += 1

        return np.frombuffer(x_mat, dtype=self[0].proto.embedding.dense.dtype).reshape(
            (len_slice, self[0].proto.embedding.dense.shape[0])
        )

    def plot_image_sprites(
        self,
        output: Optional[str] = None,
        canvas_size: int = 512,
        min_size: int = 16,
        channel_axis: int = -1,
    ):
        """Generate a sprite image for all image blobs in this DocumentArray-like object.

        An image sprite is a collection of images put into a single image. It is always square-sized.
        Each sub-image is also square-sized and equally-sized.

        :param output: Optional path to store the visualization. If not given, show in UI
        :param canvas_size: the size of the canvas
        :param min_size: the minimum size of the image
        :param channel_axis: the axis id of the color channel, ``-1`` indicates the color channel info at the last axis
        """
        if not self:
            raise ValueError(f'{self!r} is empty')

        with ImportExtensions(required=True):
            import matplotlib.pyplot as plt

        img_per_row = ceil(sqrt(len(self)))
        img_size = int(canvas_size / img_per_row)

        if img_size < min_size:
            # image is too small, recompute the size
            img_size = min_size
            img_per_row = int(canvas_size / img_size)

        max_num_img = img_per_row ** 2
        sprite_img = np.zeros(
            [img_size * img_per_row, img_size * img_per_row, 3], dtype='uint8'
        )
        img_id = 0

        actual_num_img = min(len(self), max_num_img)

        with ProgressBar(
            description='Generating sprite', total_length=actual_num_img
        ) as pg:
            for d in self:
                d: Document
                _d = Document(d, copy=True)
                if _d.content_type != 'blob':
                    _d.convert_uri_to_image_blob()
                    channel_axis = -1

                _d.set_image_blob_channel_axis(channel_axis, -1).set_image_blob_shape(
                    shape=(img_size, img_size)
                )

                row_id = floor(img_id / img_per_row)
                col_id = img_id % img_per_row
                sprite_img[
                    (row_id * img_size) : ((row_id + 1) * img_size),
                    (col_id * img_size) : ((col_id + 1) * img_size),
                ] = _d.blob

                img_id += 1
                pg.update()
                if img_id >= max_num_img:
                    break

        plt.gca().set_axis_off()
        plt.subplots_adjust(top=1, bottom=0, right=1, left=0, hspace=0, wspace=0)
        plt.margins(0, 0)
        plt.gca().xaxis.set_major_locator(plt.NullLocator())
        plt.gca().yaxis.set_major_locator(plt.NullLocator())

        plt.imshow(sprite_img)
        if output:
            plt.savefig(output, bbox_inches='tight', pad_inches=0.1, transparent=True)
        else:
            plt.show()
