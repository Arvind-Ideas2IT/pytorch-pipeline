import kfp
import json
import os
import copy
from kfp import components
from kfp import dsl
from kfp.aws import use_aws_secret
from kfp.components import load_component_from_file, load_component_from_url


cur_file_dir = os.path.dirname(__file__)
components_dir = os.path.join(cur_file_dir, "../pytorch")

bert_data_prep_op = components.load_component_from_file(
    components_dir + "/data_prep/component.yaml"
)

bert_train_op = components.load_component_from_file(
    components_dir + "/train/component.yaml"
)

download =  load_component_from_file("./Utils/download_component.yaml")
copy_contents =  load_component_from_file("./Utils/copy_component.yaml")
ls =  load_component_from_file("./Utils/list_component.yaml")

mar_op = load_component_from_file("./model_archive/component.yaml")
deploy_op = load_component_from_file("./deploy/component.yaml")

@dsl.pipeline(name="Training pipeline", description="Sample training job test")
def pytorch_bert():

    namespace = "admin"
    volume_name = "pvcm"
    model_name = "torchserve-bert"

    vop = dsl.VolumeOp(
        name=volume_name,
        resource_name=volume_name,
        modes=dsl.VOLUME_MODE_RWO,
        size="5Gi"
    )

    @dsl.component
    def download(url: str, output_path:str):
        return dsl.ContainerOp(
            name='Download',
            image='busybox:latest',
            command=["sh", "-c"],
            arguments=["mkdir -p %s; wget %s -P %s" % (output_path, url, output_path)],
        )

    @dsl.component
    def copy_contents(input_dir: str, output_dir:str):
        return dsl.ContainerOp(
            name='Copy',
            image='busybox:latest',
            command=["cp", "-R", "%s/." % input_dir, "%s" % output_dir],
        )

    @dsl.component
    def ls(input_dir: str):
        return dsl.ContainerOp(
            name='list',
            image='busybox:latest',
            command=["ls", "-R", "%s" % input_dir]
        )

    prep_output = bert_data_prep_op(
        input_data =
            [{"dataset_url":"https://kubeflow-dataset.s3.us-east-2.amazonaws.com/ag_news_csv.tar.gz"}],
        container_entrypoint = [
            "python",
            "/pvc/input/bert_pre_process.py",
        ],
        output_data = ["/pvc/output/processing"],
        source_code = ["https://kubeflow-dataset.s3.us-east-2.amazonaws.com/bert_pre_process.py"],
        source_code_path = ["/pvc/input"]
    ).add_pvolumes({"/pvc":vop.volume})

    train_output = bert_train_op(
        input_data = ["/pvc/output/processing"],
        container_entrypoint = [
            "python",
            "/pvc/input/bert_train.py",
        ],
        output_data = ["/pvc/output/train/models"],
        input_parameters = [{"tensorboard_root": "/pvc/output/train/tensorboard", 
        "max_epochs": 1, "num_samples": 150, "batch_size": 4, "num_workers": 1, "learning_rate": 0.001, 
        "accelerator": None}],
        source_code = ["https://kubeflow-dataset.s3.us-east-2.amazonaws.com/bert_datamodule.py", "https://kubeflow-dataset.s3.us-east-2.amazonaws.com/bert_train.py"],
        source_code_path = ["/pvc/input"]
    ).add_pvolumes({"/pvc":vop.volume}).after(prep_output)

    list_input = ls("/pvc/output").add_pvolumes({"/pvc":vop.volume}).after(train_output)

    properties = download(url='https://kubeflow-dataset.s3.us-east-2.amazonaws.com/model_archive/bert/properties.json', output_path="/pv/input").add_pvolumes({"/pv":vop.volume}).after(vop)
    requirements = download(url='https://kubeflow-dataset.s3.us-east-2.amazonaws.com/model_archive/bert/requirements.txt', output_path="/pv/input").add_pvolumes({"/pv":vop.volume}).after(vop)
    extrafile = download(url='https://kubeflow-dataset.s3.us-east-2.amazonaws.com/model_archive/bert/index_to_name.json', output_path="/pv/input").add_pvolumes({"/pv":vop.volume}).after(vop)
    vocabfile = download(url='https://kubeflow-dataset.s3.us-east-2.amazonaws.com/model_archive/bert/bert-base-uncased-vocab.txt', output_path="/pv/input").add_pvolumes({"/pv":vop.volume}).after(vop)
    handlerfile = download(url='https://kubeflow-dataset.s3.us-east-2.amazonaws.com/model_archive/bert/bert_handler.py', output_path="/pv/input").add_pvolumes({"/pv":vop.volume}).after(vop)

    copy_files = copy_contents(input_dir="/pvc/output/train/models", output_dir="/pvc/input").add_pvolumes({"/pvc":vop.volume}).after(train_output)
    list_input = ls("/pvc/input").add_pvolumes({"/pvc":vop.volume}).after(copy_files)

    mar_task = mar_op(
        input_dir="/pvc/input",
        output_dir="/pvc/output",
        handlerfile="image_classifier").add_pvolumes({"/pvc":vop.volume}).after(list_input)

    list_output = ls("/pvc/output").add_pvolumes({"/pvc":vop.volume}).after(mar_task)

    deploy = deploy_op(
        action="apply",
        model_name="%s" % model_name,
        model_uri="pvc://{{workflow.name}}-%s/output" % volume_name,
        namespace="%s" % namespace,
        framework='pytorch'
    ).add_pvolumes({"/pvc":vop.volume}).after(list_output)

    # Below example runs model archiver as init container for the deployer task
    # deployer_task = dsl.ContainerOp(
    #     name='main',
    #     image="quay.io/aipipeline/kfserving-component:v0.5.0",
    #     command=['python'],
    #     arguments=[
    #       "-u", "kfservingdeployer.py",
    #       "--action", "apply",
    #       "--model-name", "%s" % model_name,
    #       "--model-uri", "pvc://{{workflow.name}}-%s/output" % volume_name,
    #       "--namespace", "%s" % namespace,
    #       "--framework", "pytorch",
    #     ],
    #     pvolumes={"/pvc": vop.volume},
    #     # pass in init_container list
    #     init_containers=[
    #         dsl.UserContainer(
    #             name='init',
    #             image='jagadeeshj/model_archive_step:kfpv1.2',
    #             command=["/usr/local/bin/dockerd-entrypoint.sh"],
    #             args=[
    #                 "--output_path", output_directory,
    #                 "--input_path", input_directory,
    #                 "--handlerfile", handlerFile
    #             ],
    #             mirror_volume_mounts=True,),
    #     ],
    # ).after(list_input)


if __name__ == "__main__":
    kfp.compiler.Compiler().compile(pytorch_bert, package_path="pytorch_bert.yaml")