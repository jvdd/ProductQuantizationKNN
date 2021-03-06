# -*- coding: utf-8 -*-
__author__ = 'Jeroen Van Der Donckt'

import multiprocessing
from typing import Callable

import numpy as np
from sklearn.cluster import KMeans

from .util import log_nb_clusters_to_np_int_type, squared_euclidean_dist


class ProductQuantizationKNN:

    def __init__(self, n: int, c: int):
        """ Constructs a new instance of PQKNN.

        :param n: the amount of subvectors
        :param c: determines the amount of clusters for KMeans, i.e., k = 2**c.
        """
        self.n = n
        self.k = 2 ** c
        self.int_type = log_nb_clusters_to_np_int_type(c)
        self.subvector_centroids = {}  # Dict storing each KMeans centroids at the partition_idx, filled in compress()
        self.random_state = 420
        # The field below are initialized in the methods of ProductQuantizationKNN
        # self.partition_size = -1 # initialized in compress()
        # self.train_labels = [] # initialized in compress()
        # self.compressed_data = [[]] # initialized in compress()

    def _get_data_partition(self, train_data, partition_idx):
        partition_start = partition_idx * self.partition_size
        partition_end = (partition_idx + 1) * self.partition_size
        train_data_partition = train_data[:, partition_start:partition_end]
        return train_data_partition

    def _compress_partition(self, partition_idx: int, train_data_partition):
        km = KMeans(n_clusters=self.k, n_init=1, random_state=self.random_state) 
        compressed_data_partition = km.fit_predict(train_data_partition).astype(self.int_type)
        partition_centroids = km.cluster_centers_
        return partition_idx, compressed_data_partition, partition_centroids

    def compress(self, train_data: np.ndarray, train_labels: np.ndarray):
        """ Compress the given training data via the product quantization method.

        :param train_data: the training examples, a 2D array where each row represents a training sample.
        :param train_labels: the labels for the training data (a 1D array).
        """
        nb_samples = len(train_data)
        assert nb_samples == len(train_labels), 'The number of train samples do not match the length of the labels'
        self.train_labels = train_labels
        self.compressed_data = np.empty(shape=(nb_samples, self.n), dtype=self.int_type)

        d = len(train_data[0])
        self.partition_size = d // self.n

        with multiprocessing.Pool() as pool:
            params = [(partition_idx, self._get_data_partition(train_data, partition_idx)) for partition_idx in
                      range(self.n)]
            kms = pool.starmap(self._compress_partition, params)
            for (partition_idx, compressed_data_partition, partition_centroids) in kms:
                self.compressed_data[:, partition_idx] = compressed_data_partition
                self.subvector_centroids[partition_idx] = partition_centroids

    def predict_single_sample(self, test_sample: np.ndarray, nearest_neighbors: int,
                              calc_dist: Callable[[np.ndarray, np.ndarray], np.ndarray] = squared_euclidean_dist):
        """ Predicts the label of the given test sample based on the PQKNN algorithm

        :param test_sample: the test sample (a 1D array).
        :param nearest_neighbors: the k in kNN.
        :param calc_dist: the distance function that should be used. Defaults to squared euclidean distance.
        :return: the predicted label.
        """
        assert hasattr(self, 'compressed_data') and hasattr(self, 'train_labels'), \
            'There is no stored compressed data, therefore PQKNN can not do a k-NN search'
        # Compute table containing the distances of the sample to the centroids of each partition
        distances = np.empty(shape=(self.k, self.n), dtype=np.float64)
        for partition_idx in range(self.n):
            partition_start = partition_idx * self.partition_size
            partition_end = (partition_idx + 1) * self.partition_size
            test_sample_partition = test_sample[partition_start:partition_end]
            centroids_partition = self.subvector_centroids[partition_idx]
            distances[:, partition_idx] = calc_dist(test_sample_partition, centroids_partition)

        # Calculate (approximate) distance to stored data
        nb_stored_samples = len(self.compressed_data)
        # distance_sums = np.sum([distances[:,partition_idx][self.compressedData[:,partition_idx]] for partition_idx in range(self.n)], axis=0) -> SLOWER THAN THE CODE BELOW
        distance_sums = np.zeros(shape=nb_stored_samples)
        for partition_idx in range(self.n):
            distance_sums += distances[:, partition_idx][self.compressed_data[:, partition_idx]]

        # Select label among k nearest neighbors
        # https://stackoverflow.com/questions/34226400/find-the-index-of-the-k-smallest-values-of-a-numpy-array
        indices = np.argpartition(distance_sums, nearest_neighbors)
        labels = self.train_labels[indices][:nearest_neighbors]
        unique_labels, counts = np.unique(labels, return_counts=True)
        # 1) If there is only 1 label (among the nearest neighbors)
        if len(unique_labels) == 1:
            return unique_labels[0]
        # 2) Else -> there are 2 or more labels (among the nearest neighbors)
        sorted_idxs = np.argsort(counts)[::-1]  # Get idxs from max to min number of counts
        unique_labels = unique_labels[sorted_idxs]  # Sorted labels in descending order of their frequency
        counts = counts[sorted_idxs]  # Sorted counts in descending order of their value
        # 2.1) If there is no tie
        if counts[0] != counts[1]:
            return unique_labels[0]
        # 2.2) If there is an tie
        max_count = counts[0]
        idx = 0
        min_distance = float('inf')
        selected_label = None
        # Select label with minimal summed distance amongst the labels with frequency == max_count
        while idx < len(unique_labels) and counts[idx] == max_count:
            label = unique_labels[idx]
            label_indices = np.where(labels == label)
            summed_distance = np.sum(distance_sums[indices[label_indices]])
            if summed_distance < min_distance:
                selected_label = label
                min_distance = summed_distance
            idx += 1
        return selected_label

    def predict(self, test_data: np.ndarray, nearest_neighbors: int,
                calc_dist: Callable[[np.ndarray, np.ndarray], np.ndarray] = squared_euclidean_dist) -> np.ndarray:
        """ Predicts the label of the given test samples based on the PQKNN algorithm

        :param test_data: the samples, a 2D array where each row represents a sample.
        :param nearest_neighbors: the k in kNN.
        :param calc_dist: the distance function that should be used. Defaults to squared euclidean distance.
        :return: the predicted labels.
        """
        assert test_data.ndim == 2, 'The dimensionality of the test_data should be 2'
        if len(test_data) > 2000:  # Fuzzy rule to decide whether or not multiple threads should be spawned
            with multiprocessing.Pool() as pool:
                params = [(test_sample, nearest_neighbors, calc_dist) for test_sample in test_data]
                preds = pool.starmap(self.predict_single_sample, params)
        else:
            preds = [self.predict_single_sample(row, nearest_neighbors, calc_dist) for row in test_data]
        return np.array(preds)
