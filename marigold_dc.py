# Copyright 2024 Massimiliano Viola, Kevin Qu, Nando Metzger, Anton Obukhov ETH Zurich.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ---------------------------------------------------------------------------------
# If you find this code useful, we kindly ask you to cite our paper in your work.
# Please find bibtex at: https://github.com/prs-eth/Marigold-DC#-citation
# More information can be found at https://marigolddepthcompletion.github.io
# ---------------------------------------------------------------------------------
import argparse
import logging
import os
import warnings

import diffusers
import numpy as np
import torch
from diffusers import DDIMScheduler, MarigoldDepthPipeline
from PIL import Image

warnings.simplefilter(action="ignore", category=FutureWarning)
diffusers.utils.logging.disable_progress_bar()


class MarigoldDepthCompletionPipeline(MarigoldDepthPipeline):
    """
    Pipeline for Marigold Depth Completion.
    Extends the MarigoldDepthPipeline to include depth completion functionality.
    """  # noqa: E501

    def __call__(
        self,
        image: Image.Image,
        sparse_depth: np.ndarray,
        num_inference_steps: int = 50,
        processing_resolution: int = 768,
        seed: int = 2024,
    ) -> np.ndarray:
        """
        Args:
            image (PIL.Image.Image): Input image of shape [H, W] with 3 channels.
            sparse_depth (np.ndarray): Sparse depth guidance of shape [H, W].
            depth_range (tuple[float, float] | None, optional): Min and max depth values
                to constrain output. If None, range is determined from sparse_depth.
                Defaults to None.
            num_inference_steps (int, optional): Number of denoising steps.
                Defaults to 50.
            processing_resolution (int, optional): Resolution for processing.
                Defaults to 768.
            seed (int, optional): Random seed. Defaults to 2024.

        Returns:
            np.ndarray: Dense depth prediction of shape [H, W].

        Raises:
            ValueError: If depth_range is not a tuple of two floats with min < max.
            ValueError: If num_inference_steps is None.
            ValueError: If sparse_depth is not a 2D numpy array.
            ValueError: If sparse_depth dimensions don't match image dimensions.
        """  # noqa: E501

        # Resolving variables
        device = self._execution_device
        generator = torch.Generator(device=device).manual_seed(seed)

        # Check inputs.
        if num_inference_steps is None:
            raise ValueError("Invalid num_inference_steps")
        if type(sparse_depth) is not np.ndarray or sparse_depth.ndim != 2:
            raise ValueError(
                "Sparse depth should be a 2D numpy "
                "ndarray with zeros at missing positions"
            )

        # Prepare empty text conditioning
        with torch.no_grad():
            if self.empty_text_embedding is None:
                text_inputs = self.tokenizer(
                    "",
                    padding="do_not_pad",
                    max_length=self.tokenizer.model_max_length,
                    truncation=True,
                    return_tensors="pt",
                )
                text_input_ids = text_inputs.input_ids.to(device)
                self.empty_text_embedding = self.text_encoder(text_input_ids)[
                    0
                ]  # [1,2,1024]

        # Preprocess input images
        image, padding, original_resolution = self.image_processor.preprocess(
            image,
            processing_resolution=processing_resolution,
            device=device,
            dtype=self.dtype,
        )  # [N,3,PPH,PPW]

        # Check sparse depth dimensions
        if sparse_depth.shape != original_resolution:
            raise ValueError(
                f"Sparse depth dimensions ({sparse_depth.shape}) "
                f"must match that of the image ({image.shape[-2:]})"
            )

        # Encode input image into latent space
        with torch.no_grad():
            image_latent, pred_latent = self.prepare_latents(
                image, None, generator, 1, 1
            )  # [N*E,4,h,w], [N*E,4,h,w]
        del image

        # Preprocess sparse depth
        sparse_depth = torch.from_numpy(sparse_depth)[None, None].float().to(device)
        sparse_mask = sparse_depth > 0
        logging.info(f"Using {sparse_mask.int().sum().item()} guidance points")

        # Set up optimization targets and compute
        # the range and lower bound of the sparse depth
        scale, shift = torch.nn.Parameter(
            torch.ones(1, device=device)
        ), torch.nn.Parameter(torch.ones(1, device=device))
        pred_latent = torch.nn.Parameter(pred_latent)
        depth_min = sparse_depth[sparse_mask].min()
        depth_max = sparse_depth[sparse_mask].max()
        sparse_range = depth_max - depth_min
        sparse_lower = depth_min

        # Set up optimizer
        optimizer = torch.optim.Adam(
            [
                {"params": [scale, shift], "lr": 0.005},
                {"params": [pred_latent], "lr": 0.05},
            ]
        )

        def affine_to_metric(depth: torch.Tensor) -> torch.Tensor:
            # Convert affine invariant depth predictions to metric depth predictions
            # using the parametrized scale and shift. See Equation 2 of the paper.
            return (scale**2) * sparse_range * depth + (shift**2) * sparse_lower

        def latent_to_metric(latent: torch.Tensor) -> torch.Tensor:
            # Decode latent to affine invariant depth
            # predictions and subsequently to metric depth predictions.
            affine_invariant_prediction = self.decode_prediction(
                latent
            )  # [E,1,PPH,PPW]
            prediction = affine_to_metric(affine_invariant_prediction)
            prediction = self.image_processor.unpad_image(
                prediction, padding
            )  # [E,1,PH,PW]
            prediction = self.image_processor.resize_antialias(
                prediction, original_resolution, "bilinear", is_aa=False
            )  # [1,1,H,W]
            return prediction

        def loss_l1l2(input: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
            # Compute L1 and L2 loss between input and target.
            out_l1 = torch.nn.functional.l1_loss(input, target)
            out_l2 = torch.nn.functional.mse_loss(input, target)
            out = out_l1 + out_l2
            return out

        # Denoising loop
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        for t in self.scheduler.timesteps:
            optimizer.zero_grad()

            # Forward pass through the U-Net
            batch_latent = torch.cat([image_latent, pred_latent], dim=1)  # [1,8,h,w]
            noise = self.unet(
                batch_latent,
                t,
                encoder_hidden_states=self.empty_text_embedding,
                return_dict=False,
            )[
                0
            ]  # [1,4,h,w]

            # Compute pred_epsilon to later rescale the depth latent gradient
            with torch.no_grad():
                alpha_prod_t = self.scheduler.alphas_cumprod[t]
                beta_prod_t = 1 - alpha_prod_t
                pred_epsilon = (alpha_prod_t**0.5) * noise + (
                    beta_prod_t**0.5
                ) * pred_latent

            step_output = self.scheduler.step(
                noise, t, pred_latent, generator=generator
            )

            # Preview the final output depth with
            # Tweedie's formula (See Equation 1 of the paper)
            pred_original_sample = step_output.pred_original_sample

            # Decode to metric space, compute loss with guidance and backpropagate
            current_metric_estimate = latent_to_metric(pred_original_sample)
            loss = loss_l1l2(
                current_metric_estimate[sparse_mask], sparse_depth[sparse_mask]
            )
            loss.backward()

            # Scale gradients up
            with torch.no_grad():
                pred_epsilon_norm = torch.linalg.norm(pred_epsilon).item()
                depth_latent_grad_norm = torch.linalg.norm(pred_latent.grad).item()
                scaling_factor = pred_epsilon_norm / max(depth_latent_grad_norm, 1e-8)
                pred_latent.grad *= scaling_factor

            # Execute the update step through guidance backprop
            optimizer.step()

            # Execute update of the latent with regular denoising diffusion step
            with torch.no_grad():
                pred_latent.data = self.scheduler.step(
                    noise, t, pred_latent, generator=generator
                ).prev_sample

            del (
                pred_original_sample,
                current_metric_estimate,
                step_output,
                pred_epsilon,
                noise,
            )
            torch.cuda.empty_cache()

        del image_latent

        # Decode predictions from latent into pixel space
        with torch.no_grad():
            prediction = latent_to_metric(pred_latent.detach())

        # return Numpy array
        prediction = self.image_processor.pt_to_numpy(prediction)  # [N,H,W,1]
        self.maybe_free_model_hooks()

        return prediction.squeeze()


def main() -> None:
    parser = argparse.ArgumentParser(description="Marigold-DC Pipeline")

    DEPTH_CHECKPOINT = "prs-eth/marigold-depth-v1-0"
    parser.add_argument(
        "--in-image", type=str, default="media/image.png", help="Input image"
    )
    parser.add_argument(
        "--in-depth",
        type=str,
        default="media/sparse_100.npy",
        help="Input sparse depth",
    )
    parser.add_argument(
        "--out-depth",
        type=str,
        default="media/dense_100.npy",
        help="Output dense depth",
    )
    parser.add_argument(
        "--num_inference_steps", type=int, default=50, help="Denoising steps"
    )
    parser.add_argument(
        "--processing_resolution", type=int, default=768, help="Denoising resolution"
    )
    parser.add_argument(
        "--checkpoint", type=str, default=DEPTH_CHECKPOINT, help="Depth checkpoint"
    )
    args = parser.parse_args()

    num_inference_steps = args.num_inference_steps
    processing_resolution = args.processing_resolution
    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        if torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")
        processing_resolution_non_cuda = 512
        num_inference_steps_non_cuda = 10
        if processing_resolution > processing_resolution_non_cuda:
            logging.warning(
                f"CUDA not found: Reducing processing_resolution to "
                f"{processing_resolution_non_cuda}"
            )
            processing_resolution = processing_resolution_non_cuda
        if num_inference_steps > num_inference_steps_non_cuda:
            logging.warning(
                f"CUDA not found: Reducing num_inference_steps to {num_inference_steps_non_cuda}"
            )
            num_inference_steps = num_inference_steps_non_cuda

    pipe = MarigoldDepthCompletionPipeline.from_pretrained(
        args.checkpoint, prediction_type="depth"
    ).to(device)
    pipe.scheduler = DDIMScheduler.from_config(
        pipe.scheduler.config, timestep_spacing="trailing"
    )

    if not torch.cuda.is_available():
        logging.warning("CUDA not found: Using a lightweight VAE")
        del pipe.vae
        pipe.vae = diffusers.AutoencoderTiny.from_pretrained("madebyollin/taesd").to(
            device
        )

    pred = pipe(
        image=Image.open(args.in_image),
        sparse_depth=np.load(args.in_depth),
        num_inference_steps=num_inference_steps,
        processing_resolution=processing_resolution,
    )

    np.save(args.out_depth, pred)
    vis = pipe.image_processor.visualize_depth(
        pred, val_min=pred.min(), val_max=pred.max()
    )[0]
    vis.save(os.path.splitext(args.out_depth)[0] + "_vis.jpg")


if __name__ == "__main__":
    main()
