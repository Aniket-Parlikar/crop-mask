# The given script creates multiple processes which in turn send multiple http requests
# at a particular time to test the performance of the torchserve deployed
#  on a standalone docker container. As the number of requests increase,
# the system CPU utilization was found to reach 100%. For the scenario iwherein
# multiple models were deployed the average runtime was ~120ms.
# For single model deployed on torchserve, the execution time is ~50ms for upto 4 requests
# and then it rose to ~120ms on average for 5-10 requests.More details about the test results
# can be found in the "multi_models_logs.csv" and "Malawi_2020_September.csv".


# Importing packages
import multiprocessing
import time
import requests
import sys
# from argparse import ArgumentParse

# Defining the model url
url = "https://crop-mask-example-grxg7bzh2a-uc.a.run.app/predictions/Togo_crop-mask_2019_February"

img_list = sys.argv[1:]


# Inference function to send a request to the source url.
def inference_service(img_file):
    # start_time=time.time()
    img_url = '''gs://crop-mask-example-inference-tifs/Togo_crop-mask_2019_February/
                 min_lat=-1.63_min_lon=29.12_max_lat=4.3_max_lon=35.18_dates=2019-02-01_2020-02-01_
                 all/batch_0/'''+img_file
    response = requests.post(url, data={"uri" : img_url})
    # end_time=time.time()
    # print("The time taken to process the individual request is",end_time-start_time)
    return response.json()


if __name__ == "__main__":
    ov_stime = time.time()

    # Creating a pool object for spawning multiprocesses
    pool = multiprocessing.Pool(len(img_list))
    results = pool.map(inference_service, img_list)
    ov_etime = time.time()

    # Closing and joining the pool objects once the results are obtained
    pool.close()
    pool.join()
    # results=inference_service("10000000000-0000000000.tif")

    # Printing out the results.
    print(results)
    print(f'''For {len(img_list)} parallel requests,the program finished in {ov_etime - ov_stime}
          seconds''')
